# 00 — Measured: Open Watcom C++ builds a Win98-valid image
#
# Taken 2026-09-18, before the C-versus-C++ analysis, because the toolchain
# risk is the one that would have settled the question on its own and it is
# cheap to measure. The standing rule it tests: nothing NT-only, ever -- a
# single NT-only static import kills the image at load, before WinMain, with
# no log written and nothing to debug.
#
# ## What was built
#
# wpp386 with the same flags the C build uses, plus -xs -xr, over a source
# containing a class with a constructor, methods and a bounded string
# operation that refuses rather than truncates. Linked with the project's own
# incantation: wlink system nt_win option quiet, osversion=4.0.
#
# ## Result
#
# | gate                   | C spike            | C++ with a class      |
# |------------------------|--------------------|-----------------------|
# | PE identity            | MS Windows 4.00 GUI| MS Windows 4.00 GUI   |
# | OS / subsystem version | 4.0 / 4.0          | 4.0 / 4.0             |
# | DLLs imported          | KERNEL32 GDI32 USER32 | KERNEL32 USER32    |
# | W-suffix imports       | GetCommandLineW, GetModuleFileNameW | the same two, none added |
# | CMOV                   | 0                  | 0                     |
# | Size                   | 22,016             | 25,088                |
# | Runs                   | yes                | yes, returned 13      |
#
# The W-suffix row is the one that matters. Those two are the last unverified
# assumption in the whole toolchain per PLAN.md, they are already whitelisted,
# and C++ adds nothing to the list.
#
Fixed: SETTLES -- C++ is not rejected by the target: the image loads, the subsystem version is right, the import table is clean, and there is no CMOV. Acted on: the migration proceeded through Step 0 (extern "C" on all eleven headers, binary byte-identical at 285,904), Step 1 (win/core/rc_cpp.h plus cppcheck.exe verifying 17 guard properties under Wine), and Step 2 (sync.c -> sync.cpp, imports IDENTICAL to the C build, classifier still agreeing with bridge/paths.py across the whole corpus). Commit 3ca309a.
#
Fixed: DOES NOT SETTLE -- whether it is worth doing. Each open question is now answered rather than open. Mixing with the C miniz: miniz stays C and is reached through extern "C", which Step 0 put on every header. Global constructors under nt_win startup: they DO run before WinMain, which is before rc_log_open, so the prelude bans file-scope objects with non-trivial constructors and says why. new/delete versus the deliberate HeapAlloc discipline: operator new is defined over GetProcessHeap() so there is one allocator, and with -xd a null return skips the constructor, which preserves the existing `if (!p) return;` idiom verbatim. make check's stack-frame and committed-at-load gates: both green after the conversion, +512 bytes. Debugging without a debugger: extern "C" headers keep map symbols unmangled, so the crash-address workflow is unchanged.
#
Fixed: THE REASON THE QUESTION IS LIVE -- PLAN.md's toolchain section is meticulous about -bm, WLINK_LNK and WINVER pinning and says nothing about the language; C was inherited from retro-agent, a lift that largely did not happen. Acted on: the language choice is now a recorded decision with a measurement behind it rather than an inheritance, and the bug class that motivated it is closed at the source -- rc_strf/rc_strcpy/rc_strncpy always terminate and return whether they fit (81 sites converted, commit 756ef65), with Buf<N> carrying ok() alongside the string for the C++ modules.
#