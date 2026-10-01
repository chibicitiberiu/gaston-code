#!/usr/bin/env python3
"""The import gate: does this image load on Windows 98?

CLAUDE.md's rule is "Nothing NT-only, ever -- a single NT-only static import
kills the image at load, before WinMain, with no log written and nothing to
debug", and names `make check` as what audits imports. For a long time it did
not audit anything: the target ran

    objdump -p <exe> | grep -i 'DLL Name'
    objdump -p <exe> | ... | grep -E 'W$' || echo "   (none)"

which PRINTS the DLL list and PRINTS the W-suffix imports. Both pipelines exit
0 whatever they find, so the gate was a human reading scrollback, and in CI or
under `make all` a regression went green (audit 01.29, and 04.6/03.1 from the
other reports in docs/audit-2).

Three assertions, in the order they matter:

1. **The DLL set.** An allow-list. A new DLL is a deliberate act -- it means a
   new dependency that has to exist on the target -- so it should require
   editing this file.

2. **NT-only symbols.** A deny-list, seeded from the names this codebase's own
   comments already say it avoids, plus the obvious neighbours. This is the
   class the old `W$` grep missed entirely: `MoveFileExA` is ANSI, has no `W`
   suffix, lives in KERNEL32 and does not exist on 9x. win/sync/sync.cpp says
   in as many words that somebody will want to call it to make the commit
   atomic; when they do, this is what stops them shipping an image that dies
   at load.

   A deny-list only catches what someone thought of. The better gate is an
   allow-list generated from a real Win98 SE export dump, which is recorded as
   the next step rather than pretended to here -- see --dump.

3. **W-suffix imports.** These are Unicode stubs, present on 9x but
   non-functional. Two are known and accepted (the CRT startup calls them and
   the results are discarded); a third would be a real bug.

Exits 1 with a named failure, or 0 with a one-line summary.
"""

import re
import subprocess
import sys

# --- 1. the DLLs this image may depend on --------------------------------
#
# Adding one means the target must have it. Win98 SE ships all seven; COMCTL32
# and OLEAUT32/OLE32 are the ones a stripped install could plausibly lack, and
# both are load-bearing (the progress bar, MSHTML).
ALLOWED_DLLS = {
    "KERNEL32.DLL",
    "USER32.DLL",
    "GDI32.DLL",
    "COMCTL32.DLL",
    "WSOCK32.DLL",
    "OLE32.DLL",
    "OLEAUT32.DLL",
}

# --- 2. symbols that are NT-only, or 9x-unsafe ---------------------------
#
# Each of these either does not exist in the 9x export table (the image will
# not load) or exists as a stub that fails at runtime. Where this codebase
# already documents avoiding one, the file is named.
NT_ONLY = {
    # Named in our own comments as deliberately avoided:
    "MoveFileExA":        "win/sync/sync.cpp -- wants it for an atomic commit",
    "MoveFileExW":        "NT-only",
    "SetHandleInformation": "win/exec/execjob.cpp documents avoiding it",
    # Prevent sleep's mechanism 3 (win/remote/tray.h). Documented minimum
    # Windows XP, and whether 98's KERNEL32 exports it at all could not be
    # established from any document -- which is exactly the shape that gets
    # statically imported by a well-meaning edit and dies at load with no log.
    # Resolved with GetProcAddress in TrayService::holdSystemAwake.
    "SetThreadExecutionState": "2000/XP+; win/remote/tray.cpp resolves it",
    "GetDiskFreeSpaceExA":  "win/app/workcopy.cpp documents avoiding it",
    "GetDiskFreeSpaceExW":  "NT-only",
    # The usual neighbours a well-meaning edit reaches for:
    "CreateHardLinkA":    "NT-only",
    "CreateHardLinkW":    "NT-only",
    "GetFileAttributesExA": "NT-only",
    "GetFileAttributesExW": "NT-only",
    "TryEnterCriticalSection": "NT-only (9x exports it but it always fails)",
    "InitializeCriticalSectionAndSpinCount": "NT-only",
    "ReadDirectoryChangesW": "NT-only -- PLAN.md R6 says so",
    "CancelIo":           "NT-only",
    "GetLongPathNameA":   "NT-only",
    "GetLongPathNameW":   "NT-only",
    "SignalObjectAndWait": "NT-only",
    "SwitchToThread":     "NT-only",
    "GetSystemTimes":     "NT-only",
    "IsDebuggerPresent":  "NT-only",
    "OpenThread":         "NT-only",
    "CreateWaitableTimerA": "NT-only",
    "QueueUserAPC":       "NT-only",
    "GetProcessMemoryInfo": "PSAPI, NT-only",
    "OpenSCManagerA":     "service control, NT-only",
    "OpenSCManagerW":     "service control, NT-only",
    "CM_Get_Device_ID":   "NT-only",
    "GetGuiResources":    "NT-only",
    "AttachThreadInput":  "present on 9x but unreliable; use with care",
    "SetLayeredWindowAttributes": "NT/2000+",
    "GetConsoleWindow":   "NT-only",
    "FlushInstructionCache": "NT-only on 9x in practice",
}
# AttachThreadInput is a warn, not a fail -- it does exist on 9x.
WARN_ONLY = {"AttachThreadInput", "FlushInstructionCache"}

# --- 3. W-suffix imports that are accepted ------------------------------
#
# The CRT startup calls both and discards the result; measured in
# docs/audit/00-cpp-toolchain-measured.md and unchanged by the C++ migration.
ALLOWED_W = {"GetCommandLineW", "GetModuleFileNameW"}


def imports(exe):
    """(dlls, symbols) from objdump's import table."""
    out = subprocess.run(["objdump", "-p", exe], capture_output=True,
                         text=True, check=True).stdout
    dlls = set(re.findall(r"DLL Name:\s*(\S+)", out))
    syms = set()
    for line in out.splitlines():
        # "\t000393d8  <none>  0001  SysAllocString" -- there is an ORDINAL
        # between <none> and the name. Taking the field straight after <none>
        # collects ordinals instead of symbols, which silently made this gate
        # blind: 199 import entries came back as 96 "names" because the
        # ordinals collide across DLLs, and not one of them could ever match a
        # deny-list entry. Take the LAST field, which is what the shell
        # pipeline this replaces did with `awk '{print $NF}'`.
        m = re.match(r"\s+[0-9a-f]+\s+<none>\s+\S+\s+(\S+)\s*$", line)
        if m:
            syms.add(m.group(1))
    return dlls, syms


def version_ok(exe):
    """The PE must declare OS 4.0 / subsystem 4.0, or 9x refuses it."""
    out = subprocess.run(["objdump", "-p", exe], capture_output=True,
                         text=True, check=True).stdout
    os_v = re.search(r"MajorOSystemVersion\s+(\d+)", out)
    os_m = re.search(r"MinorOSystemVersion\s+(\d+)", out)
    ss_v = re.search(r"MajorSubsystemVersion\s+(\d+)", out)
    ss_m = re.search(r"MinorSubsystemVersion\s+(\d+)", out)
    got = tuple(int(x.group(1)) if x else -1
                for x in (os_v, os_m, ss_v, ss_m))
    return got == (4, 0, 4, 0), got


def main(argv):
    if len(argv) < 2:
        print("usage: imports.py <exe> [--dump]", file=sys.stderr)
        return 2
    exe = argv[1]
    dlls, syms = imports(exe)

    if "--dump" in argv:
        for d in sorted(dlls):
            print(d)
        for s in sorted(syms):
            print("   ", s)
        return 0

    fails = []
    warns = []

    # 1. DLLs
    extra = dlls - ALLOWED_DLLS
    if extra:
        fails.append(
            "new DLL dependency: " + ", ".join(sorted(extra)) +
            "\n     Every DLL here must exist on a stock Win98 SE. If this one"
            "\n     does, add it to ALLOWED_DLLS in tools/check/imports.py and"
            "\n     say why in the commit.")

    # 2. NT-only symbols
    for s in sorted(syms):
        if s in NT_ONLY:
            why = NT_ONLY[s]
            if s in WARN_ONLY:
                warns.append("%s -- %s" % (s, why))
            else:
                fails.append(
                    "NT-ONLY IMPORT: %s (%s)"
                    "\n     This kills the image at LOAD on Win98, before"
                    "\n     WinMain, with no log written and nothing to debug."
                    "\n     Resolve it with LoadLibrary/GetProcAddress instead."
                    % (s, why))

    # 3. W-suffix
    bad_w = {s for s in syms if s.endswith("W")} - ALLOWED_W
    if bad_w:
        fails.append(
            "unexpected W-suffix import: " + ", ".join(sorted(bad_w)) +
            "\n     Unicode entry points are stubs on 9x. Only %s are"
            "\n     accepted (the CRT calls them and discards the result)."
            % ", ".join(sorted(ALLOWED_W)))

    # 4. PE version
    ok, got = version_ok(exe)
    if not ok:
        fails.append(
            "PE version is OS %d.%d / subsystem %d.%d, want 4.0 / 4.0"
            "\n     Anything higher and Win98 refuses to load the image."
            % got)

    for w in warns:
        print("   warn: %s" % w)
    if fails:
        for f in fails:
            print("   FAIL: %s" % f)
        return 1
    print("   ok: %d symbols from %d DLLs, no NT-only imports, PE 4.0/4.0"
          % (len(syms), len(dlls)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
