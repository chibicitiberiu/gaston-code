# What was measured, not argued

Everything in this file was produced by running something. The commands are
given so a reader can disagree with the result rather than with the claim.

## 1. What Open Watcom's C++ front end actually accepts

The recommendations in `04-recommendations.md` are worth nothing if the
compiler refuses them. `rc_cpp.h` says the dialect is "C++98/03 as Open
Watcom implements it. No nullptr, no move semantics, **no templates beyond
the trivial**, no exceptions (-xd), no RTTI." The "beyond the trivial" part
was never pinned down, so it was probed directly, under the **shipping
flags** including `-wx` (warnings are errors) and `-xd`.

| construct | accepted? |
|---|---|
| class template with a policy/traits type parameter (`Guard<T, Policy>`) | yes |
| member function template (`template <class U> U *as(...)`) | yes |
| partial specialisation (`template <class T> struct Traits<T *>`) | yes |
| default template argument (`template <class T, class P = GdiPolicy>`) | yes |
| non-type template parameter (`Buf<N>`, already shipping) | yes |
| function template with argument deduction | yes |
| class template as a base class | yes |
| nested class inside a template | yes |
| empty class with only a destructor | yes |

All nine compiled clean, zero warnings:

    wpp386 -bt=nt -bm -zq -otexan -5 -wx -xd -DWINVER=0x0400 \
           -D_WIN32_WINNT=0x0400 -DWIN32_LEAN_AND_MEAN ...

**The dialect is not the constraint.** A policy-parameterised guard —
`Guard<HGDIOBJ, DeleteObjectPolicy>` — is available today. `rc_cpp.h`'s
"no templates beyond the trivial" understates what the compiler will take,
and if it is meant as a *house rule* rather than a *compiler limit* it
should say so, because the two lead to different decisions.

### 1b. The standard smart pointers

| | result |
|---|---|
| `std::unique_ptr` | **does not exist** — `E926: 'std::unique_ptr' has not been declared` |
| `std::auto_ptr` + `<memory>` | compiles and links clean, including under `-wx` |

Cost of `auto_ptr` against the house `HeapPtr` for the same job, linked into
otherwise identical images:

| | `_TEXT` |
|---|---|
| `HeapPtr<T>` | 18,968 |
| `std::auto_ptr<T>` | 19,096 |

128 bytes more, and it can only own `delete`-able pointers — not a `HANDLE`,
`HGDIOBJ`, `FILE *`, `BSTR`, or a filename. See the note at the top of
`04-recommendations.md`.

## 2. What a guard costs, in bytes

This is the constraint, and it is not the one the comments anticipate.

A function acquiring a DC, a font and a brush, with five exit paths, written
twice — once releasing by hand, once with three scope guards — then linked
into otherwise identical images:

| version | the function | its guards' code | total |
|---|---|---|---|
| hand-written | 322 B | — | **322 B** |
| guarded | 544 B | 118 B | **662 B** |

Whole-image `_TEXT` delta: **+400 bytes**. The guarded build also pulls in a
library module the manual one does not (`plibmt3r.lib(undefed.cpp)`,
`__wcpp_4_undefed_cdtor__`).

**Open Watcom does not inline these destructors.** They are emitted as
out-of-line `__near` functions and called. This is not an artefact of the
optimisation level — the sweep below changes the code size and never changes
the four out-of-line constructor/destructor bodies:

| flags | `_TEXT` | out-of-line cdtors |
|---|---|---|
| `-otexan` (shipping) | 19,528 | 4 |
| `-otexan -oe=100` | 19,528 | 4 |
| `-otexan -oi` | 19,528 | 4 |
| `-oxe -oe=100` | 19,352 | 4 |
| `-os` | 19,272 | 4 |

So on this toolchain RAII is a real trade, not a free one: correctness on
paths nobody wrote yet, paid for in code bytes. That is still usually worth
it, but it has to be *chosen* per site rather than applied everywhere, and
the budget has to come from somewhere. See `04-recommendations.md`.

### 2b. What the guards already in the image cost

Measured from `build/win/retroclaude.map`, summing each guard member
function to the next symbol:

| guard | bytes emitted |
|---|---|
| `ComPtr<T>` | 496 |
| `HeapPtr<T>` | 240 |
| `Bstr` | 64 |
| `ScopedLock` | 32 |
| **total** | **832** |

`ComPtr<T>` is instantiated **eight times** — `IDispatch`,
`IHTMLDocument2`, `IHTMLElement`, `IHTMLElement2`, `IHTMLElementCollection`,
`IHTMLWindow2`, `IOleInPlaceObject`, `IOleWindow` — and every instantiation
emits its own constructor, destructor, `reset` and `get`. They are
near-identical: `Release` is the third slot of every COM vtable, which is the
guarantee `comptr.h`'s own header comment relies on to justify the cast to
`IUnknown`. **That same guarantee means the code does not need to be
duplicated per type.** A non-template core holding `IUnknown *`, with a thin
typed wrapper over it, would emit the release logic once. See
recommendation R6.

## 3. Static allocation, measured

`make check` on the current build:

    DGROUP   image  121180  file   30208  bss   90972
    total committed at load: 372060 bytes

`_BSS` from the linker map is **88,412 bytes** (`0x1595c`).

The map names only 16 BSS symbols, because file-scope `static`s are local and
carry no name into it — so a per-symbol ranking cannot be read out of the
map, and any attempt to compute one by symbol gaps over-attributes wildly.
Sizing the declarations instead, every static array of 256 bytes or more in
shipping code (`win/`, excluding `third_party/` and `checks/`):

| bytes | where | declaration |
|---|---|---|
| 66,560 | `net/net.cpp:742` | `static char build[RC_MAX_FRAME + 1024]` |
| 8,192 | `rpc/exec.cpp:44` | `static char g_chunk[EXEC_CHUNK]` |
| 2,048 | `app/tooldet.cpp:195` | `static char g_rows[2048]` |
| 2,048 | `app/tooldet.cpp:15` | `static char g_sections[2048]` |
| 260 | `sync/syncrpc.cpp:69` | `static char g_home[MAX_PATH]` |
| 260 | `sync/syncrpc.cpp:35` | `static char g_root[MAX_PATH]` |
| 260 | `rpc/rpc.cpp:56` | `static char g_watch_root[MAX_PATH]` |
| 260 | `rpc/rpc.cpp:53` | `static char g_sync_root[MAX_PATH]` |
| 260 | `core/log.cpp:11` | `static char g_path[MAX_PATH]` |
| 260 | `app/workdir.cpp:18` | `static char g_current[MAX_PATH]` |
| 260 | `app/main.cpp:224` | `static char g_exe_dir[MAX_PATH]` |
| | | **80,668 total** |

> **Superseded 2026-10-11, and the top two rows are gone.** The 66,560-byte
> buffer moved to the heap, per connection, with the libwire extraction
> (`win/wire/connection.cpp:925-933`), and `app/main.cpp:224`'s `g_exe_dir`
> became a member when the app became a lifecycle. Measured on the exes at
> `01b5254`: gaston's `_BSS` is **13,816 bytes**, not 88,412, with no symbol in
> it above 2,000. The table below is kept as what was true on 2026-09-21.
>
> The warning above it is still exactly right, and `tools/check/mapsize.py`
> was written to obey it: symbol-gap attribution over-attributes wildly, so
> that tool reports the data segments as unattributed and splits any single
> charge over 2 KB into an `unnamed` column rather than crediting it to the
> module that happens to precede it.

Eleven declarations account for 91% of BSS, and **one of them accounts for
75%**. `docs/BACKLOG.md` §2 already owns this and names the same two
buffers; the number there is "86 KB", which is now 88,412 bytes. Nothing in
this audit changes that conclusion — it is recorded here so the RAII
discussion is not mistaken for the memory discussion. **They are separate
problems.** Scope guards do not move BSS, and the 400-byte code cost of a
guard is paid in `AUTO`, not `DGROUP`.

## 4. Stack frames

The 4096-byte-per-frame gate passes: `make check` reports "no frame in our
own code reaches 4096 bytes". Every frame it does report belongs to
`miniz.o` and is marked not reachable from our code.

## 5. Reproducing all of this

    make -C win all
    make -C win check
    python3 -c "..."        # see 00-manifest.md for the exact tree state

The template probe and the guard-cost comparison were built in a scratch
directory against the same compiler and flags; both are small enough to
retype from the tables above.
