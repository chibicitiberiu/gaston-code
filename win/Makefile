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
         -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt" -i="$(CURDIR)" \
         -i="$(CURDIR)/third_party/miniz"

LFLAGS = system nt_win option quiet, osversion=4.0

# wsock32 rather than ws2_32: we use the Winsock 1.1 API surface, and
# wsock32.dll is present on every target from Win95 onward (on Win98 it is a
# forwarder to ws2_32). comctl32 is for the status bar.
LIBS   = library wsock32.lib, comctl32.lib, ole32.lib, oleaut32.lib, uuid.lib

OBJS = main.o net.o frameproto.o log.o ini.o htmlview.o miniz.o \
       exec.o rpc.o sync.o syncrpc.o

.PHONY: synccheck all clean check run netcheck probe htmlspike

# Spike: a minimal MSHTML host in plain C. Answers whether the transcript can
# move to HTML, which the 512 KB measured cost says is affordable.
HTMLSPIKE_OBJS = htmlspike.o
htmlspike: htmlspike.exe
htmlspike.exe: $(HTMLSPIKE_OBJS)
	@$(WLINK) $(LFLAGS) library ole32.lib, oleaut32.lib, uuid.lib \
	  name $@ file {$(HTMLSPIKE_OBJS)}
	@echo "built $@"
	@objdump -p $@ | grep -i 'DLL Name'

# Phase 0 guest experiments. Console subsystem so it can be run from a DOS box
# and its output read without a GUI.
SYNCCHECK_OBJS = synccheck.o sync.o log.o miniz.o
synccheck: synccheck.exe
synccheck.exe: $(SYNCCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(SYNCCHECK_OBJS)}
	@echo "built $@"

PROBE_OBJS = probe.o miniz.o
probe: probe.exe
probe.exe: $(PROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(PROBE_OBJS)}
	@echo "built $@"
	@echo "--- import audit (an NT-only symbol here would kill it at load) ---"
	@objdump -p $@ | grep -i 'DLL Name'
	@objdump -p $@ | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print $$NF}' \
	  | grep -E 'SetHandleInformation|OpenSCManager|CM_Get|GetGuiResources' \
	  && echo "  ^^ NT-ONLY IMPORT -- WILL NOT LOAD ON WIN9X" || echo "  clean"

all: retroclaude.exe

# Headless conformance check: links the REAL net.c/frameproto.c so it exercises
# the same code the GUI does, without needing GUI automation.
# net.o now calls into the RPC worker, so netcheck must link it too or
# the build breaks with unresolved externals.
NETCHECK_OBJS = netcheck.o net.o frameproto.o log.o miniz.o \
                exec.o rpc.o sync.o syncrpc.o
netcheck: netcheck.exe
netcheck.exe: $(NETCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(NETCHECK_OBJS)}
	@echo "built $@"

# Every object depends on every header. Coarse, but there are seven of them
# and the failure it prevents is vicious: adding two fields to rc_transcript
# did not rebuild main.o, so main.c still allocated the OLD struct while
# transcript.c wrote the new fields past the end of it, corrupting the next
# global. The symptom was PostMessage failing on a valid HWND -- nowhere near
# the change, and it looked exactly like a Win9x message-queue limit.
HDRS = $(wildcard *.h)

%.o: %.c $(HDRS)
	@$(WCC) $(CFLAGS) -fo=$@ $<

miniz.o: third_party/miniz/miniz.c third_party/miniz/miniz.h
	@$(WCC) $(CFLAGS) -fo=$@ third_party/miniz/miniz.c

retroclaude.exe: $(OBJS)
	@$(WLINK) $(LFLAGS) $(LIBS) name $@ file {$(OBJS)}
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# Known-good CMOV count. See the note in the check target: this is a data
# false positive, not a real instruction. Any INCREASE is a real regression.
CMOV_BASELINE = 1
PYTHON ?= python3

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
	@echo "=== CMOV (a Pentium MMX has none; 686+ only) ==="
	@# objdump disassembles the whole AUTO section linearly, and that section
	@# holds read-only data as well as code -- so string tables decode as
	@# instructions and produce phantom CMOVs. There is one such false
	@# positive today, at a repeating data pattern near 0x416d00 where the
	@# surrounding "disassembly" is `push %cs; inc %ebx`, which no compiler
	@# emits. Gating on 0 would therefore fail forever and the gate would
	@# start being ignored, which is worse than not having it. Gate on the
	@# baseline instead, so a real regression still trips it.
	@n=$$(objdump -d -m i386 retroclaude.exe 2>/dev/null | grep -cE '\scmov'); \
	 if [ "$$n" -gt $(CMOV_BASELINE) ]; then \
	   echo "   FAIL: $$n CMOV (baseline $(CMOV_BASELINE)) -- check -5 is still set"; exit 1; \
	 else echo "   ok: $$n (baseline $(CMOV_BASELINE), known data false positive)"; fi
	@echo "=== static allocation (the guest has 28KB free after a turn) ==="
	@# Uninitialised statics are committed at load and are spent before the
	@# app does anything -- rc_net_send's frame build buffer alone is 66,560
	@# bytes. `objdump -h` reports the RAW section size, which omits BSS
	@# entirely and makes this look 7x smaller than it is, so read the PE
	@# section headers directly.
	@$(PYTHON) -c "import struct;d=open('retroclaude.exe','rb').read();\
pe=struct.unpack_from('<I',d,0x3c)[0];\
n=struct.unpack_from('<H',d,pe+6)[0];o=pe+24+struct.unpack_from('<H',d,pe+20)[0];\
rows=[(d[o+i*40:o+i*40+8].rstrip(bytes(1)).decode(),)+struct.unpack_from('<IIII',d,o+i*40+8) for i in range(n)];\
[print('   %-8s image %7d  file %7d  bss %7d' % (r[0],r[1],r[3],max(0,r[1]-r[3]))) for r in rows];\
print('   total committed at load: %d bytes' % sum(max(r[1],r[3]) for r in rows))"

clean:
	@rm -f *.o retroclaude.exe
