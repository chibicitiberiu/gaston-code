# Measured: Open Watcom C++ builds a Win98-valid image

Taken 2026-09-18, before the C-versus-C++ analysis, because the toolchain risk
is the one that would have settled the question on its own and it is cheap to
measure. The standing rule it tests:

> **Nothing NT-only, ever.** A single NT-only static import kills the image at
> load, before `WinMain`, with no log written and nothing to debug.

## What was built

`wpp386` with the same flags the C build uses, plus `-xs -xr`, over a source
containing a class with a constructor, methods and a bounded string operation
that refuses rather than truncates. Linked with the project's own incantation:
`wlink system nt_win option quiet, osversion=4.0`.

## Result

| gate | C spike | C++ with a class |
|---|---|---|
| PE identity | `MS Windows 4.00 (GUI)` | `MS Windows 4.00 (GUI)` |
| OS / subsystem version | 4.0 / 4.0 | 4.0 / 4.0 |
| DLLs imported | KERNEL32, GDI32, USER32 | KERNEL32, USER32 |
| W-suffix imports | `GetCommandLineW`, `GetModuleFileNameW` | **the same two, none added** |
| CMOV (Pentium MMX has none) | 0 | **0** |
| Size | 22,016 | 25,088 |
| Runs | yes | yes — returned 13, `lstrlenA("C:\WORK\a.txt")` |

The W-suffix row is the one that matters. Those two are the last unverified
assumption in the whole toolchain per `PLAN.md`, they are already whitelisted,
and C++ adds nothing to the list. The load-time hazard the standing rule exists
for is measured and absent.

## What this does and does not settle

**Settles:** C++ is not rejected by the target. The image loads, the subsystem
version is right, the import table is clean, and there is no CMOV.

**Does not settle:** whether it is worth doing. Mixing with the C `miniz`,
global constructors under `nt_win` startup, `new`/`delete` versus the
deliberate `HeapAlloc` discipline, the effect on `make check`'s stack-frame and
committed-at-load gates, and debugging on a machine with no debugger are all
open. See `05-c-versus-cpp.md`.

## The reason the question is live

`PLAN.md`'s "Toolchain facts" is meticulous -- `-bm`, `WLINK_LNK`, `WINVER`
pinning, `_WIN32_IE` defaults -- and **says nothing about the language**. C came
from `retro-agent`, a 25k-line C codebase this project intended to lift from;
that lift largely did not happen, and `main.c`, `net.c`, `sync.c`, `syncrpc.c`
and `htmlview.c` are all original.

Meanwhile the nine defects fixed on 2026-09-18 were mostly one bug class:
silent truncation, from `_snprintf(x, sizeof(x) - 1, ...)` with the return
value discarded, at roughly 85 call sites. The worst of them destroyed the
user's file. A bounded-string type that refuses instead of truncating would
make that class unrepresentable rather than auditable -- and that is precisely
the class compiled above.
