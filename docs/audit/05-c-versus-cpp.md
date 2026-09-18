# 05 — C89 or C++ for the guest

*2026-09-18. Every number here was measured on this machine with this
toolchain; the commands are in the appendix so they can be re-run.*

## Recommendation

**Proceed, as a staged boundary migration. Do not do a whole-tree rewrite, and
do not start with `htmlview.c`.**

The language was never argued in `PLAN.md` — the toolchain section is four
pages of measured detail and the word "C" appears only as an input. It was
inherited from `retro-agent`, and the lift it was inherited for did not happen:
`main.c`, `net.c`, `sync.c`, `syncrpc.c` and `htmlview.c` are all original
work. So there is no sunk-cost argument, only a technical one.

The technical one comes out in favour, and by a wider margin than I expected,
because the two decisive risks turned out to be measurably absent:

- **The load-time risk is zero.** A mixed C/C++ `retroclaude.exe` imports the
  same seven DLLs, the same **172 symbols**, the same two W-suffix imports and
  has the same CMOV count as the C build. Nothing new appears in the import
  table. The image that kills at load before `WinMain` does not exist here.
- **The conversion cost is 18 errors across 6,898 lines.** Eight of the ten
  guest modules — including `main.c`, `net.c`, `sync.c` and `syncrpc.c` —
  compile under `wpp386` with **zero errors and zero new warnings**, unchanged.

What tips it is the bug history. Of the fourteen commits landed on 2026-09-18,
three are silent-truncation defects in the guest, one of which destroyed the
user's file. All three are the same shape, and a bounded-string type makes that
shape a compile-time-enforced runtime refusal instead of silence.

**Start with `sync.c`, not `htmlview.c`** — see §3. That is the one place my
conclusion disagrees with the request.

---

## What was measured

| Question | Method | Result |
|---|---|---|
| Does the tree compile as C++? | `wpp386` over all ten `win/*.c` | 8 of 10 clean; 18 errors total |
| Does a mixed binary link and run? | 8 TUs C++, 3 C, `wlink system nt_win` | Links first try; runs under Wine, window up, INI read, reconnect backoff correct |
| Import-table delta | `objdump -p` | **172 symbols both ways.** Same 7 DLLs, same 2 W-imports, CMOV 1 both |
| Image size | `stat` | 197,120 → 206,848 (**+9,728, +4.9%**) |
| Committed at load | PE section headers | 285,904 → 293,664 (**+7,760, +2.7%**); BSS actually *fell*, 89,808 → 87,840 |
| `make check` stack-frame gate | `tools/stackframes.py` | Green. Same exempt list, no new frame in our code |
| Do global ctors run under `nt_win`? | probe writing to a log from a static object's ctor | **Yes**, before `WinMain`; dtors run at exit |
| Does `new` drag in a second heap? | import diff | Yes by default — but a 6-line `operator new` over `GetProcessHeap()` removes it entirely and *shrinks* the probe from 30,720 to 26,112 bytes |
| Does the ctor run if `operator new` returns NULL? | probe with a failing allocator | **No.** Watcom emits the null check. `new` behaves exactly like `HeapAlloc` |
| miniz | already has `#ifdef __cplusplus extern "C"` at `miniz.h:228` | Compile it as C, include it from C++, link: works, CRC verified against zlib |
| Map readability | `grep` the linker map | Public symbols stay unmangled with `extern "C"` headers; statics never appeared in either map |
| Dialect | probes | **C++98/03.** Templates yes. `nullptr` no, rvalue references no, sized delete no, `<string>` does not compile under `-xd` |

---

## 1. What C++ buys, ranked by bug class removed

### 1.1 A bounded string type — this is the whole case

**The numbers.** 95 `_snprintf` calls across `win/*.c`. Exactly **two** use the
return value, both in `exec.c:116` and `exec.c:118`. The other 93 discard it.
43 sites follow up with an explicit `x[sizeof(x) - 1] = '\0'`. The
`sizeof(x) - 1` idiom appears 89 times.

The idiom prevents the overrun and guarantees the truncation. That is a
deliberate trade and it was the wrong one, three times today.

**The three commits.**

`5cfc712` — *A long path made the guest destroy the file it was writing.*
`rc_sync_begin_write` built `<target>.gaston-tmp` and `rc_sync_commit` built
`<target>.gaston-old` with truncating `_snprintf` into `MAX_PATH`. Between 251
and 257 characters the two working names truncate to the *same string*, so
`rc_sync_commit` deleted its own verified temp file, moved the user's file
aside and straight back, and **returned 0** — the guest acked `ok` with the CRC
it had been sent, and the bridge advanced its baseline over bytes the machine
never took. At 258 and 259 the temp name resolves to the target itself (Win32
strips a trailing dot) and `CreateFileA(CREATE_ALWAYS)` truncated the user's
file in place, before the compare-and-swap that exists to protect it had run.

`b14be6d` — *A directory at the path limit vanished, or multiplied.* `walk()`
built its search pattern with `join()` then `strncat(pattern, "\\*", ...)`.
One byte short: the backslash lands, the star does not, `FindFirstFileA` fails,
**the entire subtree leaves the manifest** and the bridge reads it as the user
having deleted every file in it. Exactly full: `strncat` appends nothing,
`FindFirstFileA` succeeds on the bare directory, returns the directory's own
entry, and `walk` recurses on the same truncated pattern to
`RC_SYNC_MAX_DEPTH`, inventing a chain of directories the bridge then creates
for real with `os.makedirs`.

`657cd9d` — *The user's own message was cut at 4095 bytes without saying so.*
`GetWindowTextA` copies what fits and reports the shortened length; the input
control had no `EM_LIMITTEXT`. A pasted log went to the model shortened, with
no copy of the rest anywhere. `esc_html` had the same bug one line later.

**Would a type have made it unrepresentable?** Not unrepresentable. *Loud*,
which is what was actually missing. I built the type and ran the real bug:

```cpp
template <unsigned N> class Buf {
    char b[N]; unsigned n; bool bad;
public:
    Buf() : n(0), bad(false) { b[0] = '\0'; }
    bool ok() const { return !bad; }
    Buf &cat(const char *s) {
        unsigned l = (unsigned)strlen(s);
        if (bad || n + l >= N) { bad = true; return *this; }
        memcpy(b + n, s, l); n += l; b[n] = '\0'; return *this;
    }
};
typedef Buf<MAX_PATH> Path;
```

Fed the 5cfc712 input (a 251-character relative path under `C:\p`) it prints:

```
target ok=1 len=256 | tmp ok=0 len=256
```

The target forms; the working name refuses and says so. The `bad` flag is
**sticky**, which is the property that matters: an overflow twelve `cat()`s ago
is still visible at the end, so a builder cannot half-succeed. And `n + l >= N`
refuses at equality, which is precisely the case `b14be6d` says a checked
`strncat` would have missed — *"A checked strncat that refuses only on overflow
catches the first and not the second, because the second is the equality
case."*

Note what this does **not** do. The full fix for 5cfc712 was a new HELLO
capability (`workreserve`), declared by the client, so the bridge stops short
*before pushing*. A type does not invent that protocol; it turns a
silent-destruction bug into a visible refusal, and the design work of deciding
what to do about the refusal remains. That is still the difference between
losing the user's file and logging a line — which is the standing rule this
project is built on: *the person at the keyboard never loses a byte.*

The C89 alternative exists — a checked `rc_cat()` returning an error — and
would catch the same three. The reason to prefer the type is that `Buf` makes
the check the *only* way to build a path, while `rc_cat()` sits beside
`_snprintf` and `strncat` in a codebase that has 95 of the one and reaches for
it by habit. `sync.c:231`'s `join()` returns `void` today: it structurally
cannot report the failure that caused two of the three bugs.

**Verdict: the strongest single argument, and on its own sufficient.**

### 1.2 RAII for paired operations — real, second by margin

Counted in `win/`:

| pair | acquire | release | note |
|---|---|---|---|
| `HeapAlloc` / `HeapReAlloc` → `HeapFree` | 13 | 26 | **twice as many releases as acquisitions** — the imbalance *is* the finding |
| `EnterCriticalSection` → `LeaveCriticalSection` | 13 | 18 | five extra exits, all on error paths |
| `CreateFileA` / `CreatePipe` / `CreateEventA` / `_beginthreadex` / `CreateProcessA` → `CloseHandle` | 13 | 25 | `exec.c` alone holds 12 of the closes |
| `FindFirstFileA` → `FindClose` | 2 | 2 | balanced; both in `sync.c` |

Counted over the ten modules that link into `retroclaude.exe`, comments
stripped. `probe.c` is excluded — it is a diagnostic tool, not shipped.

I looked for actual leaks and **did not find one**. The code is correct today.
What it costs to be correct is the finding.

`rc_sync_stat_file` (`win/sync.c:123-204`) allocates one scratch block and
frees it on **four** separate paths — alloc-fail, open-fail, read-fail, and the
normal exit — each a hand-written `HeapFree(GetProcessHeap(), 0, sc)`. One
`HeapPtr sc(...)` and it is one line, unmissable.

The reader loop (`reader_thread`, `win/net.c:118-261`) is the worst case. One `rc_frame *f`
with an **ownership transfer** — to the UI thread via `PostMessage`, or to the
RPC worker via `rc_rpc_submit` — and **eight** distinct free sites for it,
across parse failure, inflate failure, EXECKILL, EXEC, SYNC, submit failure
and PostMessage failure. Every one of those is a place a future `break` leaks a
heap block on a machine with 28 KB free. The comment at `net.c:230` says so
outright: *"Freeing it here is the whole reason the inflight counter exists."*
A `FramePtr` with an explicit `release()` at the two transfer points reduces
eight sites to two, and the two that remain are the ones that are genuinely
interesting.

`rc_net_send` (`win/net.c:597-654`) has **five** `LeaveCriticalSection` calls
in one function — build-failure, alloc-failure, the backpressure wait
(unlock/wait/relock), the `!running` bail, and the success path. `BACKLOG.md`
§1 and §5 both flag this function as still having live defects: a soft byte
cap, a 5-second UI-thread block, and `n->sock` read unserialised across four
threads at 14 sites. A scoped lock with an explicit `unlock()`/`relock()` for
the wait would not fix the *design* problems, but it removes the class where
somebody adds a sixth early return and forgets the fifth `Leave`.

`exec.c:130-188` is the handle case, and it is the ugliest code in the tree for
a reason that has nothing to do with the author. Two `CreatePipe` calls produce
four handles; the failure paths close *different subsets* of them — `rd, wr` at
`:151`, `rd` alone at `:165`, `rd, wr, in_rd` at `:184` — and two more closes
at `:154` and `:187` are deliberate, because closing the write end is how you
get EOF when the child exits. Twelve of the shipped binary's twenty-five `CloseHandle`
calls are in this one function. `PLAN.md` §3.2 already lists two
known leaks in the same file, forked in from `retro-agent`: `HeapReAlloc`
failure leaking the original block at two sites. A `Handle` with an explicit
`close()` keeps the two deliberate early closes readable as intent and makes
the three error paths automatic.

**Verdict: real, not cosmetic, and it compounds.** But note it is prevention of
bugs this codebase has *not yet had*, whereas §1.1 is repair of bugs it has had
three of today. Ranked second on evidence, not on taste.

### 1.3 Encapsulation for `rc_html` / `rc_net` / `rc_sync` — mostly cosmetic

`htmlview.h` is already the pattern: `typedef struct rc_html rc_html;` plus
`create` / `destroy` / `move` / `poll` / `append` / `live` / `clear`. The
struct is opaque, the invariants hold, `htmlview.c` is the only translation
unit that can reach the fields. That is encapsulation. Making it a class
changes `rc_html_append(v, s)` to `v->append(s)` and little else.

The honest gain is the **destructor**, not the access control — `rc_html`
holds an MSHTML instance, an `IOleObject`, an `IOleInPlaceSite` and a heap
queue, and its teardown ordering is the kind of thing a `~Html()` gets right
by construction. That is §1.2 again, arriving through a different door.

The real counter-evidence: **`win/htmlview.c` has one commit in its entire
history and no bug fixes.** The files with bug histories are `main.c` (19
commits), `net.c` (12), `sync.c` (7), `syncrpc.c` (5). The module that most
*looks* like it wants to be a class is the one that has never gone wrong.

**Verdict: the weakest of the three. Do not justify the migration on it.**

---

## 2. What it costs

Everything below is measured, not estimated.

**Size: +9,728 bytes of image, +7,760 bytes committed at load (+2.7%).** That
is the real mixed build, not a spike. Against a 32 MB guest where `BACKLOG.md`
§2 records 2,312 KB free at window creation, it is 0.3% of the headroom. BSS
went *down* 1,968 bytes, because more of the same data landed in the
initialised section. `-xd` (exceptions and RTTI off) is doing the work here:
the earlier `-xs -xr` probe was 39,936 bytes against 30,720 for the same source
with `-xd` — **exception support alone is 9.2 KB**, most of the reported 3 KB
delta and then some. Use `-xd`. Nothing in the guest wants exceptions on a
machine where the response to OOM is "report unreadable and carry on".

**miniz: a non-issue.** `third_party/miniz/miniz.h:228` already carries
`#ifdef __cplusplus extern "C" {`. Compile `miniz.c` with `wcc386` as now,
include `miniz.h` from a `.cpp`, link. Verified end to end: `mz_crc32("hello")`
returned 907060870, which matches Python's `zlib.crc32`. Compiling miniz *as*
C++ fails with 2 errors (`const object must be initialized`, and
`s_tdefl_num_probes` already defined — C tentative definitions are not a C++
thing), so don't; there is no reason to.

**Static constructors do run under `nt_win`.** Verified: a file-scope object's
constructor wrote its log line before `WinMain` did, and its destructor ran at
exit. That is a *hazard*, not a benefit, on this project. `log.c` opens
`retroclaude.log` from the first statement of `WinMain` deliberately, because
"no log file created" is the primary signal that `main()` never ran. A static
constructor that faults runs *before* that, with no log, which reproduces
exactly the debugging position that `PLAN.md` builds the whole logging
discipline to avoid. **Rule: no file-scope object with a non-trivial
constructor.** POD statics only, same as today. Worth an addition to `make
check` — a grep is enough.

**`new`/`delete`: fixable, and the fix shrinks the binary.** By default OW's
`new` goes through the CRT's `malloc`, which drags in `TlsAlloc`/`TlsFree`/
`TlsGetValue`/`TlsSetValue`, `InitializeCriticalSection` and friends. On
inspection that objection evaporates: `build/win/retroclaude.exe` **already
imports all of them**, plus `VirtualAlloc`, because `-bm` and `_beginthreadex`
already link that CRT. Nothing new arrives. But the project uses `HeapAlloc`
deliberately and a second arena is a second fragmentation profile, so override
it:

```cpp
void *operator new(size_t n)   { return HeapAlloc(GetProcessHeap(), 0, n ? n : 1); }
void *operator new[](size_t n) { return operator new(n); }
void  operator delete(void *p)   { if (p) HeapFree(GetProcessHeap(), 0, p); }
void  operator delete[](void *p) { operator delete(p); }
```

Six lines. The probe went from 30,720 to **26,112** bytes and its imports went
back to `GetProcessHeap`/`HeapAlloc`/`HeapFree`. (Sized `operator delete` is
C++14 and is rejected — this compiler is C++98/03.)

**And OOM stays safe.** I checked the one thing that would have been
disqualifying: with a null-returning `operator new` and no exceptions, does the
constructor run on a null `this`? **It does not.** Watcom emits the null check
and the new-expression yields null. So `if (!p) return;` — the discipline
already written 15 times against `HeapAlloc` — carries over verbatim.

**`make check` gates: all four stay green.**
- *Imports*: identical. 172 symbols, 7 DLLs, `GetCommandLineW` and
  `GetModuleFileNameW` and nothing else W-suffixed.
- *CMOV*: 1 in both, the known data false positive.
- *Stack frames*: green, same exempt list (miniz's zip reader/writer and
  `clib3r.lib:___begtext`), nothing in our own code at 4096.
- *Committed BSS*: +7,760, and the tool reports it, so the regression is
  visible rather than guessed — which is what `BACKLOG.md` §2 asks for.

**Debugging: smaller than feared, but real.** The crash workflow is "invalid
page fault at 0167:00416d07" → look the address up in `retroclaude.map`. With
`extern "C"` on the module headers, public symbols stay unmangled — I checked
the C++ build's map and it reads `rc_net_send_`, `rc_net_start_`,
`rc_net_stop_`, exactly as the C map does. Statics never appear in *either*
map, so `stackframes.py` already resolves to the nearest public symbol and
loses nothing. What does degrade: **any new class method is mangled**, and a
fault inside one resolves to a Watcom-mangled name. `tools/stackframes.py`'s
`\S+` regex still matches it, so the gate keeps working; only the reading gets
uglier. `wdis` is available in `binl64` if it ever matters.

**`-wx` gets noisier.** C++ enables `W836: symbol has different type than
previous symbol in same declaration` — `syncrpc.c:353` and `:393` trip it on
`char full[MAX_PATH], *q;`. Two sites, cosmetic, and `-wx` is a warning level
rather than `-we`, so nothing fails.

**The headers need `extern "C"`.** None of the ten `win/*.h` have it. Without
it a `.cpp` including `net.h` mangles `rc_net_send` and fails to link against a
C-built `net.o`. The failure is a loud undefined symbol at link, which is the
safe kind. Four lines per header, mechanical, and it is the enabling step for
everything else.

**The 18 errors, in full.**
- `frameproto.c`: 2. Both `nl = memchr(...)`, at `:55` and `:68`, where `nl`
  is `char *`. C++ will not implicitly convert `void *`. Two casts.
- `syncrpc.c`: 0 errors, 2 warnings.
- `htmlview.c`: 16, and all one family — hand-rolled COM vtables initialised
  with `(void *)site_QueryInterface, (void *)site_AddRef, ...` (`:93`, `:126`,
  `:176`), and `(void **)` casts into `QueryInterface`. C++ refuses `void *` →
  function pointer in an initialiser. The C code is erasing its own type
  checking to build those tables; C++ demands the real types. Fixing it
  properly means turning three vtables into three classes, which is a rewrite
  and not a conversion.

---

## 3. Whole tree, new code only, or one module?

**One module first, as a probe. And the module is `sync.c`, not
`htmlview.c`.**

*Against all-at-once.* There is no technical blocker — I built the mixed
binary and it ran — but 18 errors concentrated entirely in one file means a
whole-tree flag day is really "convert nine files trivially and rewrite
`htmlview.c`'s COM layer", and putting those in one commit makes the rewrite
invisible inside the noise. This project's git history is one defect per
commit with the reasoning in the message; a flag day is the opposite of that.

*Against new-code-only.* It gives the worst of both: two idioms in one tree
with no rule for which applies where, and it leaves `sync.c` — the file that
produced two of today's three truncation bugs — in the dialect that produced
them. It also never pays back the `extern "C"` work.

*Why `sync.c` rather than `htmlview.c`.* Four reasons, in order of weight:

1. **It is where the bug class lives.** `5cfc712` and `b14be6d` are both
   `sync.c`; `cc24892` is `sync.c` feeding `syncrpc.c`. `htmlview.c` has one
   commit and zero bugs.
2. **It compiles as C++ today with zero errors and zero warnings.** So the
   probe measures the *idiom*, not the conversion. `htmlview.c` measures the
   conversion and tells you nothing about whether `Path` was worth it.
3. **It has a standalone harness that runs on the guest.** `synccheck.exe` is
   a console binary linking the real `sync.o`, and `synccheck --limits` already
   prints the path boundary the guest enforces, diffed against `bridge/caps.py`
   by `tests/python/test_guest_agreement.py`. You can verify the C++ `sync.o`
   on the real Windows 98 box without touching the GUI. Nothing else in the
   tree has that.
4. **`htmlview.c` is the highest-risk conversion in the tree and the lowest
   reward.** It is the only file with real errors, its errors are in COM
   vtables, and MSHTML hosting is the part of the guest that has never been
   debugged under load. Converting it means rewriting the thing that works.

The user's instinct that `htmlview` wants to be a class is right about the
*shape* and wrong about the *order*. It should be last, and it should be a
rewrite done on purpose, not a migration step.

---

## 4. What does not change, and must not

Nothing in this document touches any of these, and the migration is only
correct if they stay true:

- **The guest transforms nothing.** No markdown, no JSON, no OCR, no image
  work moves guest-side because C++ makes it easier to write. `Buf<N>` is not
  a licence to build a string library; it is a licence to stop truncating
  paths. Compression remains the single exception.
- **Nothing NT-only, ever.** Measured: 172 imports, identical set. `make
  check`'s import audit stays the gate, and it stays the *first* gate.
- **Bounded and small allocations.** `operator new` over `GetProcessHeap()`,
  no `std::string`, no `std::vector`, no container that grows on its own. The
  28 KB figure is softer than it was cited as (`BACKLOG.md` §2 corrects it —
  `dwAvailPhys` excludes the disk cache), but the discipline is right anyway.
- **`_beginthreadex`, never raw `CreateThread`.** Unaffected; `-bm` is
  unchanged.
- **`make check` green, every gate.** Verified against the C++ build already.
  Add one gate: no file-scope object with a non-trivial constructor.
- **No exceptions, no RTTI.** `-xd`. A failed allocation returns null and is
  reported, exactly as now.
- **Verify on the guest, not under Wine.** Everything above is Wine or static
  analysis. Wine proves the code; it never proves the platform. Step 2 below
  does not close until `synccheck.exe` has run on the real box.

---

## 5. The migration plan

Each step is independently shippable, independently revertable, and gated on
`make check` plus its own harness.

**Step 0 — `extern "C"` on all ten `win/*.h`. No language change.**
Four lines per header inside the existing include guard. The tree still builds
entirely with `wcc386`; nothing behaves differently. *Verify:* `make check`
byte-identical output, binary size unchanged. *Rollback:* revert one commit,
it touches only headers.

**Step 1 — `win/rc_cpp.h` and the build rule. No module converted.**
The prelude: `operator new`/`delete` over `GetProcessHeap()`; `Buf<N>` and
`typedef Buf<MAX_PATH> Path`; `ScopedLock`; `Handle` (CloseHandle); `FindHandle`
(FindClose); `HeapPtr<T>` with `release()`. All C++98 — no `nullptr`, no move
semantics, private copy-ctor for the non-copyables. Add `%.o: %.cpp` to
`win/Makefile` with `-xd` added to `CFLAGS`. Land it with one throwaway `.cpp`
that exercises each guard, plus a unit binary in the `synccheck.exe` mould.
*Verify:* the throwaway runs under Wine and each destructor fires in order.
*Rollback:* delete two files and one Makefile rule.

**Step 2 — `sync.c` → `sync.cpp`. The probe.**
Rename, compile, fix nothing (it is already clean). Then, in a *second*
commit, convert `join()` from `void` to returning `Path`, and route
`rc_sync_begin_write` and `rc_sync_commit` through it, deleting the two
hand-written length checks added by `5cfc712` in favour of `ok()`. Convert
`rc_sync_stat_file`'s four `HeapFree` paths to one `HeapPtr`, and `walk`'s
`FindFirstFileA` to `FindHandle`. *Verify:* `tests/python/test_guest_agreement.py`
still diffs `synccheck --limits` against `bridge/caps.py` and agrees;
`synccheck.exe` runs **on the real Win98 box** and reports 248 allowed / 249
refused, the boundary `5cfc712` measured; `make check` green on all four gates.
*Rollback:* `git mv sync.cpp sync.c`, revert the two commits. Nothing else in
the tree knows the difference, because of Step 0.

**Step 3 — `syncrpc.cpp`, then `net.cpp`.**
Both already compile clean. `net.c` is the payoff: the reader loop's eight free
sites become a `FramePtr` with two `release()` calls, and `rc_net_send`'s five
`LeaveCriticalSection` become a `ScopedLock`. Do **not** fold in the
`BACKLOG.md` §5 `n->sock` serialisation fix here — that is a separate defect
with its own commit, and mixing it in makes the migration un-revertable.
*Verify:* `netcheck.exe`, which links the real `net.o` and `frameproto.o`;
then a live session against the bridge on the guest. *Rollback:* per file.

**Step 4 — `main.cpp`, `rpc.cpp`, `exec.cpp`, `log.cpp`, `ini.cpp`.**
All clean today. `main.c` is 1,783 lines and 19 commits of history, so it is
last among the clean ones and gets its own commit. `inicheck.exe` covers
`ini.c`. *Verify:* full app on the guest, plus the memory measurement
`BACKLOG.md` §2 asks for — window created / chat list loaded / after one turn,
against 2,312 / 960 KB.

**Step 5 — `frameproto.cpp`.** Two casts. Trivial, kept separate only so it is
not lost inside Step 3.

**Step 6 — `htmlview.c`, by decision, later, and as a rewrite.**
Not a migration step. Reopen it when the COM layer needs to change for its own
reasons — it has one commit and no bugs, so there is no reason yet. If and when
it happens, the three `(void *)`-erased vtables become three classes with real
method signatures, which is a genuine improvement in a file where the compiler
currently checks nothing.

**Never:** `miniz.c`. It stays C, it has `extern "C"` already, and compiling it
as C++ costs 2 errors for zero benefit.

### Rollback, overall

Because of Step 0 every module keeps a C ABI, so the tree can sit in a mixed
state indefinitely and any single file can go back to C with a rename and a
revert. There is no point in the plan where the build is C++-or-nothing. That
property is the reason to do Step 0 first and separately.

---

## Appendix — reproducing the measurements

```sh
export WATCOM=/usr/bin/watcom WLINK_LNK=$WATCOM/binl64/wlink.lnk
CF="-bt=nt -bm -zq -otexan -5 -wx -xd -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 \
    -DWIN32_LEAN_AND_MEAN -i=$WATCOM/h -i=$WATCOM/h/nt"

# error count per module
for f in win/*.c; do cp $f /tmp/$(basename ${f%.c}).cpp; \
  $WATCOM/binl64/wpp386 $CF -fo=/tmp/o.o /tmp/$(basename ${f%.c}).cpp 2>&1 \
  | grep -c ': Error!'; done

# the mixed binary: 8 TUs C++, htmlview/frameproto/miniz C, then
$WATCOM/binl64/wlink system nt_win option quiet, osversion=4.0, map=rc.map ...

# import-table delta
objdump -p X.exe | grep -cE '^\s+[0-9a-f]+\s+<none>'      # 172 both ways
objdump -p X.exe | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print $NF}' | grep 'W$'

# committed at load — same arithmetic make check uses
make -C win check
```

The static-constructor, `operator new`, null-`new` and `Buf<N>` probes are
four small `.cpp` files; each writes a log line and is run under the project's
existing Wine prefix at `spike/.wineprefix` with `winecfg -v win98`.
