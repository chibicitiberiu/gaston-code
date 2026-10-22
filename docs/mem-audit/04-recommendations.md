# What to change, and what to leave alone

> **Status, 2026-09-21.** The guard *mechanism* now exists in
> `win/core/rc_cpp.h`: `Unique<T, Policy>` plus `Handle`, `FindHandle`,
> `GdiObj`, `MemoryDC`, `File`, `Dll`, `MenuGuard`, `WindowGuard`, and the
> three that need more than the resource itself — `WindowDC`, `Selected`,
> `TempFile`. `UniqueObj<T>`/`UniqueArr<T>` cover `delete`/`delete[]`.
> Seventeen cases were added to `tests/guest/cppcheck.cpp` (34 pass), and
> `TempFile`'s F1 case was verified to go RED when the bug is reintroduced.
> **No call site has been converted yet**, so `retroclaude.exe` is unchanged
> and the defects below are all still live. R1 and R2 are now edits to
> `syncrpc.cpp`/`sync.cpp` and `cache.cpp`/`export.cpp`, not new types.

Ranked by defects-prevented per byte of code added. Every cost figure comes
from `01-measurements.md`: a guard class costs roughly 20-50 bytes for its
out-of-line destructor plus roughly 40 bytes at each site that uses it,
because **Open Watcom does not inline these destructors at any optimisation
level.** That is the budget every item below is spending.

Two facts frame the whole list:

1. **The dialect is not the limit.** Policy-parameterised templates, partial
   specialisation, member templates and default template arguments all
   compile under the shipping flags. `rc_cpp.h`'s "no templates beyond the
   trivial" is a house rule, not a compiler limit, and it should say which it
   is.
2. **Almost everything is already C++.** Only `core/path.c` and `core/str.c`
   are C, and neither owns a resource. Nothing is blocked by language.

---

## A note on "smart pointers", since that was the question

The standard ones are not the answer here, and it is worth saying why with
numbers rather than by asserting that the toolchain is old.

`std::unique_ptr` **does not exist** — this is C++98, and the compiler
rejects it outright (`E926: 'std::unique_ptr' has not been declared`). The
C++98 smart pointer is `std::auto_ptr`, and that one genuinely does compile
and link under the shipping flags, including `-wx`. I measured it against
the house `HeapPtr` doing the same job:

| | `_TEXT` |
|---|---|
| `HeapPtr<T>` (this codebase's own) | 18,968 |
| `std::auto_ptr<T>` + `<memory>` | 19,096 |

128 bytes more, for a type with two disqualifying problems. It manages only
`delete`-able pointers — it cannot hold a `HANDLE`, an `HGDIOBJ`, a `FILE *`,
a `BSTR` or a temp file's *name*, which between them are every resource in
`02-findings.md`. And its copy constructor **transfers ownership silently**,
which is the behaviour it was deprecated for and the opposite of what this
codebase wants: every guard in `rc_cpp.h` is deliberately non-copyable,
because "a copied guard releases twice, which for `CloseHandle` on a recycled
handle is worse than leaking."

So the recommendation is not "adopt smart pointers". It is **extend the set
of guards that already exists**, which is the right shape for this problem:
the resources here are Win32 handles and paired API calls, not heap objects,
and `HeapPtr`/`Handle`/`ScopedLock`/`ComPtr` already establish the house
pattern. Every item below is one more member of that family.

---

## R1. A guard for the sync temp file — fixes F1 and F2

**The highest-value item in this report.** F1 leaks a temp file on every
ordinary edit-conflict rejection; F2 leaks one on a stat failure. Both are
the same shape: one resource, two release actions, decoupled.

```cpp
class TempFile {              /* owns the NAME, not the handle */
    char path[MAX_PATH];
    int  armed;
public:
    explicit TempFile(const char *p);          /* armed = 1 */
    ~TempFile() { if (armed) DeleteFileA(path); }
    void keep() { armed = 0; }                 /* it committed; hands off */
    const char *get() const { return path; }
};
```

The point is what it owns. Today the deletion is keyed on the *handle* being
live, which is why closing the handle early at `syncrpc.cpp:423` silently
disarmed it. A guard keyed on the *file* cannot be disarmed by accident — only
by `keep()`, at the one place the file is genuinely no longer ours.

Cost: ~30 bytes of destructor, two or three sites. Removes five leaking paths
in `do_putend` and one in `rc_sync_commit`, and makes a seventh — the next one
someone adds — impossible rather than merely absent.

Do this one whether or not anything else on this list happens.

## R2. A `FILE *` guard — 24 hand-managed sites, none covered

`rc_cpp.h` guards `HANDLE`, `FindFirstFile` handles and heap blocks, and has
nothing for `FILE *`. There are **24 `fclose` calls** in the app layer:
fifteen in `cache.cpp`, four in `export.cpp`, five in `htmlview.cpp`.

`cache.cpp` writes `if (X_fp) { fclose(X_fp); X_fp = NULL; }` four separate
times as a "force-close whatever the last caller left open" guard. That idiom
existing four times is the signal: the type is not carrying its own
invariant.

Note the wrinkle, because it is why this is R2 and not R1: `export.cpp:233`
captures `fclose`'s **return value** (`int closed = fclose(fp);`) because
stdio defers a full-disk failure into the close. A guard whose destructor
discards that return would lose a real error. So it needs a `close()` that
returns the status for the sites that care, with the destructor as the
backstop for the sites that only need not to leak — the same shape as
`Handle::release()`.

## R3. A `FramePtr` — six sites of a two-step free

```c
rc_frame_free(f);                      /* the payload and inflated body */
HeapFree(GetProcessHeap(), 0, f);      /* the struct */
```

appears verbatim at `rpc.cpp:119, 125, 146, 460` and `net.cpp:150`. Where a
guard *is* used, at `net.cpp:240`, it is `HeapPtr<rc_frame>` — which owns
only the struct, so `rc_frame_free(f.get())` still has to be called by hand
beside it (`net.cpp:253, 300`). That is a guard at the wrong granularity:
ownership of one logical object is split across two mechanisms, and its own
comment has to explain that the guard "owns just the rc_frame struct, never
`payload`".

One `FramePtr` doing both in its destructor, with `release()` for the two
genuine transfers, collapses all six and removes the split.

Doing this also naturally fixes **F4**: `HeapPtr<char> payload(...)` with a
`release()` on the line before `rc_frame_parse` puts the ownership transfer
where it actually occurs, instead of in a callee's first statement with two
differently-correct error paths nine lines apart.

## R4. `ScopedLockIf` — the conditional-lock pattern, five sites

`if (g_ready) Enter...; ...; if (g_ready) Leave...` at `log.cpp:58/64`,
`rpc.cpp:249/251` and `rpc.cpp:291/293`. A five-line class stores the flag's
value once at construction, which also closes the half of **F6** where
`rc_log_close` can flip `g_ready` between a thread's two tests.

## R5. `Handle` and `FindHandle`, where the cleanup is unconditional

`FindHandle` exists in `rc_cpp.h` and is used **zero times**. The two
`FindFirstFileA`/`FindClose` pairs in `sync.cpp` (`:449/548` and `:722/775`)
span 99 and 53 lines each, with a recursive call in the middle. They are
correct today — every early exit inside the loop is a `continue` or `break`,
never a `return` — and that is exactly the property that a future edit
breaks without noticing.

`exec.cpp` is the other candidate: six `CloseHandle` calls across four paths
managing four pipe handles and two process handles. **I traced every path and
they are all correct.** Converting it buys nothing today and buys the next
early return, which in that function is a three-handle leak.

Both are worth doing, and neither is urgent. They are listed below R1-R4
because they fix nothing that is currently broken.

## R6. Give `ComPtr<T>` a non-template core — saves ~370 bytes of BSS-adjacent code

Measured: `ComPtr<T>` is instantiated **eight times** and emits **496 bytes**
of near-identical code. `comptr.h`'s own header explains why it does not have
to be: *"Every COM vtable begins with QueryInterface, AddRef and Release in
that order, whatever the interface -- that is what makes the cast to IUnknown
below legal rather than merely convenient."*

That guarantee means the release logic can live once, in a non-template base
holding `IUnknown *`, with `ComPtr<T>` reduced to typed `get()`/`operator->`
that the compiler can fold. Roughly 130 bytes instead of 496.

This is the one item that *saves* bytes rather than spending them, which is
what makes it the natural funding source for R1-R4 on a machine with 28KB
free.

## R7. Where a guard would be actively wrong — do not convert these

Recording this is as important as the list above, because a later pass with
an RAII checklist will otherwise "fix" them.

- **`rc_net_stop`'s teardown (`net.cpp:957-1007`).** Cleanup here is
  *conditional on the join results*: the socket, the critical section and
  both events are deliberately **leaked** when a thread might still be using
  them, because "a leaked descriptor at process exit costs nothing; one freed
  under a live `send()` is a frame written into whatever the OS handed out
  next." A destructor fires unconditionally. Wrapping these in `Handle` would
  convert a carefully chosen leak into a use-after-free on exactly the path
  the current code exists to survive.
- **The three deliberately-leaked critical sections** (`rpc.cpp:815-824`,
  `exec.cpp`, `net.cpp:997-1000`), for the same reason.
- **The temporary unlock inside a `ScopedLock`** (`net.cpp:799-802`). Correct
  and already explained in place.
- **`log.cpp`'s `g_fh` as a raw global `HANDLE`.** `rc_cpp.h`'s standing rule
  is no file-scope objects with non-trivial constructors, because static
  ctors run before `WinMain` and therefore before the log exists. The logger
  is the one thing that must work when nothing else does.
- **Resource icons.** Never `DestroyIcon`'d, deliberately, documented twice.
  A guard here would be a double free.

The general rule this suggests, worth writing into `rc_cpp.h`: **a guard is
right where the release is unconditional. Where releasing is a decision, the
decision needs to stay visible at the call site.**

---

## Suggested order

| | item | fixes | net bytes |
|---|---|---|---|
| 1 | R6 — `ComPtr` non-template core | — | **−370** |
| 2 | R1 — `TempFile` | F1, F2 | +100 |
| 3 | R3 — `FramePtr` | F4, six duplicated pairs | +150 |
| 4 | R4 — `ScopedLockIf` | half of F6 | +60 |
| 5 | R2 — `FILE *` guard | 24 sites, cache.cpp's four-times idiom | +200 |
| 6 | R5 — `FindHandle`, `Handle` in `exec.cpp`/`sync.cpp` | nothing today | +250 |

R6 first is deliberate: it pays for the next three.

## The defects that no guard fixes

F3, F5, F7 and most of F6 are **ordering and synchronisation** bugs, not
lifetime bugs. They want four one-line changes, not a type:

- **F3** — put `inflate_finish()` inside the join-gate two lines above it.
- **F5** — have `rc_net_start` refuse, or clean up, when it finds a
  connection whose teardown was abandoned.
- **F6** — move `FlushFileBuffers` out of the lock, or accept the contention
  deliberately and say so; take `g_lock` around `g_fh`'s close.
- **F7** — give `g_home` the treatment `g_root` already has, seventeen lines
  above it, and make `rc_syncrpc_root()` use `root_snapshot()`.

These are cheaper than everything in R1-R6 and they fix live bugs. **Do them
first.**
