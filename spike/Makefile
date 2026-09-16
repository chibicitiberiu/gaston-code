# Win98 GUI smoke test. Two toolchains, same source.
#
#   make ow       Open Watcom 2.0 (installed at /usr/bin/watcom)   [WORKS]
#   make mingw    mingw-w64 cross from Fedora repos                [untested]
#   make check    static PE audit: OS version, imports, CMOV, size
#   make wine        run under Wine (reuses Lutris wine-ge, version set to win98)
#   make wine-smoke  non-interactive: alive after 6s == pass

WATCOM ?= /usr/bin/watcom
OWBIN   = $(WATCOM)/binl64
# wlink searches PATH (not WATCOM) for its wlink.lnk config, which is what
# defines the "nt_win" system. Calling it by absolute path is NOT enough:
# without OWBIN on PATH you get "undefined system name: nt_win" plus a wall
# of undefined __imp__ symbols. WATCOM itself is needed to locate lib386/.
export WATCOM
export PATH := $(WATCOM)/binl64:$(WATCOM)/binl:$(PATH)
WCC     = $(OWBIN)/wcc386
WLINK   = $(OWBIN)/wlink

# -bt=nt   target Win32
# -5       Pentium scheduling (no CMOV; that is 686+)
# -otexan  optimise for time/size, no stack frames
OWCFLAGS = -bt=nt -bm -zq -otexan -5 -wx -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt"

# osversion=4.0 puts "Windows 4.00" in the PE header, which is the Win95/98/NT4 gate
OWLFLAGS = system nt_win option quiet, osversion=4.0

MCC     = i686-w64-mingw32-gcc
MCFLAGS = -Wall -Wextra -Os -DWINVER=0x0410 -D_WIN32_WINNT=0x0410 \
          -DWIN32_LEAN_AND_MEAN -D__USE_MINGW_ANSI_STDIO=0 \
          -march=i586 -mtune=pentium3 -fno-stack-protector \
          -fno-exceptions -fomit-frame-pointer -DNDEBUG
MLFLAGS = -static -s -mwindows -Wl,--subsystem,windows:4.0 \
          -Wl,--major-os-version,4 -Wl,--minor-os-version,0

all: ow

ow: hello98.c
	$(WCC) $(OWCFLAGS) hello98.c
	$(WLINK) $(OWLFLAGS) name hello98.exe file hello98.o
	@rm -f hello98.o

mingw: hello98.c
	$(MCC) $(MCFLAGS) -o hello98.exe hello98.c $(MLFLAGS)

check:
	@echo "=== identity ==="
	@file hello98.exe
	@echo
	@echo "=== PE version gate (want OS 4.0 / Subsystem 4.0) ==="
	@objdump -p hello98.exe | grep -iE 'OSystemVersion|SubsystemVersion|^Subsystem'
	@echo
	@echo "=== imported DLLs (MSVCRT.DLL here means a CRT dependency to audit) ==="
	@objdump -p hello98.exe | grep -i 'DLL Name'
	@echo
	@echo "=== every imported symbol ==="
	@objdump -p hello98.exe | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print "   "$$NF}'
	@echo
	@echo "=== W-suffix imports (stubs on Win9x; fine if version-guarded, fatal if not) ==="
	@objdump -p hello98.exe | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print $$NF}' | grep -E 'W$$' || echo "   (none)"
	@echo
	@echo "=== CMOV count (must be 0 for Pentium-1 class hardware) ==="
	@objdump -d -m i386 hello98.exe 2>/dev/null | grep -cE '\scmov' || echo 0
	@echo
	@echo "=== size ==="
	@ls -l hello98.exe | awk '{print "   "$$5" bytes"}'

# Reuse an existing Wine instead of installing one system-wide. Lutris's
# wine-ge build is a standalone Wine (not Proton) with full 32-bit support
# (lib/wine/i386-unix + i386-windows), which is what a win32 prefix needs.
# Override with:  make wine WINEDIR=/path/to/some/wine
WINEDIR ?= $(HOME)/.local/share/lutris/runners/wine/wine-ge-8-26-x86_64
WINE     = $(WINEDIR)/bin/wine

# Prefix kept inside the project so it never touches ~/.wine and is disposable.
WINEPREFIX := $(CURDIR)/.wineprefix
export WINEPREFIX
export WINEARCH  := win32
export WINEDEBUG ?= -all
export PATH      := $(WINEDIR)/bin:$(PATH)

# Filters Wine's harmless startup/shutdown noise.
WNOISE = grep -vE 'RLIMIT_NICE|client error|using server-side sync' || true

$(WINEPREFIX):
	@echo ">> creating win32 prefix at $(WINEPREFIX)"
	@wineboot -i 2>&1 | $(WNOISE)
	@winecfg -v win98 2>&1 | $(WNOISE)
	@echo ">> prefix reports:"
	@wine reg query 'HKLM\Software\Microsoft\Windows\CurrentVersion' /v VersionNumber 2>/dev/null | tail -2

# Wine validates that the CODE is sane. It does NOT validate Win98 support:
# Wine fully implements W-functions that real Win98 only stubs, and its
# comctl32 is a reimplementation. 'make check' is the load-time gate; a real
# Win98 guest is the behavioural one.
wine: hello98.exe | $(WINEPREFIX)
	@$(WINE) --version
	$(WINE) hello98.exe 2>&1 | $(WNOISE)

# Non-interactive smoke test for CI: alive after N seconds == pass.
wine-smoke: hello98.exe | $(WINEPREFIX)
	@echo ">> running for 6s; staying alive is a pass"
	@timeout 6 $(WINE) hello98.exe >/dev/null 2>&1; 	  if [ $$? -eq 124 ]; then echo "PASS: survived 6s in its message loop"; 	  else echo "FAIL: exited early"; exit 1; fi

wine-clean:
	rm -rf $(WINEPREFIX)

clean:
	rm -f hello98.exe hello98.o

.PHONY: all ow mingw check wine wine-smoke wine-clean clean
