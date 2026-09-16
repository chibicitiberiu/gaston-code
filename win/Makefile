# retroclaude.exe -- the single Win98 guest binary.
#
# One executable, one TCP connection, three threads (UI / reader / RPC worker).
# See ../docs/PLAN.md.

TOP     := $(CURDIR)/..
WATCOM  ?= /usr/bin/watcom
OWBIN    = $(WATCOM)/binl64

WCC   = $(OWBIN)/wcc386
WLINK = $(OWBIN)/wlink
WRC   = $(OWBIN)/wrc

# wlink finds wlink.lnk (which defines `nt_win`) via PATH, not via $WATCOM and
# not via its own directory. WLINK_LNK is stricter and immune to another
# Watcom earlier on PATH.
export WATCOM
export WLINK_LNK = $(OWBIN)/wlink.lnk

# -bm  multi-threaded CRT (reader + RPC worker threads). Necessary but not
#      sufficient: use _beginthreadex, never raw CreateThread, or the CRT
#      per-thread block is never allocated or released. CreateThread with
#      lpThreadId=NULL also fails outright on Win95/98.
# -5   Pentium scheduling; keeps CMOV (686+) out of the image.
# WINVER pinned at 0x0400: raising it silently changes sizeof(NONCLIENTMETRICS)
#      and breaks SPI_GETNONCLIENTMETRICS on the guest.
# _WIN32_IE left at its 0x0400 default so TBSTYLE_* stays available.
CFLAGS = -bt=nt -bm -zq -otexan -5 -wx \
         -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 \
         -DWIN32_LEAN_AND_MEAN \
         -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt" -i="$(CURDIR)"

LFLAGS = system nt_win option quiet, osversion=4.0

# wsock32 rather than ws2_32: we use the Winsock 1.1 API surface, and
# wsock32.dll is present on every target from Win95 onward (on Win98 it is a
# forwarder to ws2_32). comctl32 is for the status bar.
LIBS   = library wsock32.lib, comctl32.lib

OBJS = main.o net.o frameproto.o log.o ini.o transcript.o

.PHONY: all clean check run netcheck
all: retroclaude.exe

# Headless conformance check: links the REAL net.c/frameproto.c so it exercises
# the same code the GUI does, without needing GUI automation.
NETCHECK_OBJS = netcheck.o net.o frameproto.o log.o
netcheck: netcheck.exe
netcheck.exe: $(NETCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(NETCHECK_OBJS)}
	@echo "built $@"

%.o: %.c
	@$(WCC) $(CFLAGS) -fo=$@ $<

retroclaude.exe: $(OBJS)
	@$(WLINK) $(LFLAGS) $(LIBS) name $@ file {$(OBJS)}
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# Static PE audit. The import table is the gate that matters: a single NT-only
# symbol kills the EXE at load on Win98, before WinMain, with no log written.
check: retroclaude.exe
	@echo "=== identity ==="; file retroclaude.exe
	@echo "=== version gate (want 4.0/4.0) ==="
	@objdump -p retroclaude.exe | grep -iE 'OSystemVersion|SubsystemVersion'
	@echo "=== DLLs ==="; objdump -p retroclaude.exe | grep -i 'DLL Name'
	@echo "=== W-suffix imports (stubs on Win9x) ==="
	@objdump -p retroclaude.exe | grep -E '^\s+[0-9a-f]+\s+<none>' \
	  | awk '{print $$NF}' | grep -E 'W$$' || echo "   (none)"
	@echo "=== CMOV (must be 0) ==="
	@objdump -d -m i386 retroclaude.exe 2>/dev/null | grep -cE '\scmov' || echo 0

clean:
	@rm -f *.o retroclaude.exe
