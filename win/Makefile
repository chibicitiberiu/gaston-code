# retroclaude.exe -- the single Win98 guest binary.
#
# One executable, one TCP connection, three threads (UI / reader / RPC worker).
# See ../docs/PLAN.md.

TOP     := $(CURDIR)/..
WATCOM  ?= /usr/bin/watcom
OWBIN    = $(WATCOM)/binl64

# Every generated file goes here and nothing generated goes anywhere else.
# Source directories stay readable: `ls win/` used to show fourteen .o files,
# six .exe files, a .map, a stray .err and an orphaned transcript.o whose
# source had been deleted months earlier. One gitignored tree also means
# `rm -rf build` is a complete clean with nothing left behind.
BUILD   ?= $(TOP)/build/win

WCC   = $(OWBIN)/wcc386
WPP   = $(OWBIN)/wpp386
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
# -xd: no exceptions, no RTTI. Exception support alone measured 9.2 KB, and
# this codebase has no use for it -- the discipline is "return null and let the
# caller decide", which C++ preserves because with -xd and a null-returning
# operator new, Watcom SKIPS the constructor.
CXXONLY = -xd

CFLAGS = -bt=nt -bm -zq -otexan -5 -wx \
         -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 \
         -DWIN32_LEAN_AND_MEAN -DTDEFL_LESS_MEMORY=1 \
         -i="$(CURDIR)" -i="$(CURDIR)/third_party/miniz" \
         -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt"

LFLAGS = system nt_win option quiet, osversion=4.0

# wsock32 rather than ws2_32: we use the Winsock 1.1 API surface, and
# wsock32.dll is present on every target from Win95 onward (on Win98 it is a
# forwarder to ws2_32). comctl32 is for the status bar.
LIBS   = library wsock32.lib, comctl32.lib, ole32.lib, oleaut32.lib, uuid.lib

OBJS = $(addprefix $(BUILD)/,main.o bar.o chatlist.o xferstat.o pagebar.o \
       optmenu.o input.o hostdlg.o toolbar.o export.o cache.o workdir.o workcopy.o workdlg.o tooldet.o net.o \
       frameproto.o log.o ini.o str.o path.o htmlview.o miniz.o exec.o rpc.o \
       sync.o syncrpc.o)

.PHONY: synccheck inicheck cppcheck framecheck all clean check run netcheck rpccheck probe htmlspike capscheck workcheck toolcheck

# Without this, make's default goal is the FIRST target in the file, which is
# htmlspike -- a Phase 0 spike. `make -C win` therefore printed "Nothing to be
# done for 'htmlspike'" and exited 0 while building nothing, which reads
# exactly like a successful no-op build of the real thing. Same class of trap
# as the stale check binaries described above `all`.
.DEFAULT_GOAL := all

# Spike: a minimal MSHTML host in plain C. Answers whether the transcript can
# move to HTML, which the 512 KB measured cost says is affordable.
HTMLSPIKE_OBJS = $(BUILD)/htmlspike.o
htmlspike: $(BUILD)/htmlspike.exe
$(BUILD)/htmlspike.exe: $(HTMLSPIKE_OBJS)
	@$(WLINK) $(LFLAGS) library ole32.lib, oleaut32.lib, uuid.lib \
	  name $@ file {$(HTMLSPIKE_OBJS)}
	@echo "built $@"
	@objdump -p $@ | grep -i 'DLL Name'

# Phase 0 guest experiments. Console subsystem so it can be run from a DOS box
# and its output read without a GUI.
SYNCCHECK_OBJS = $(addprefix $(BUILD)/,synccheck.o sync.o log.o str.o path.o miniz.o)
synccheck: $(BUILD)/synccheck.exe
$(BUILD)/synccheck.exe: $(SYNCCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(SYNCCHECK_OBJS)}
	@echo "built $@"

# Asserts save() and load() agree on the field list. Two bugs of that exact
# shape have shipped (font_size, then the sync root), and neither was visible
# from reading the code.
INICHECK_OBJS = $(addprefix $(BUILD)/,inicheck.o ini.o log.o str.o)
inicheck: $(BUILD)/inicheck.exe
$(BUILD)/inicheck.exe: $(INICHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(INICHECK_OBJS)}
	@echo "built $@"

# The frame header contract, run against the shipping frameproto.o. Exists
# because frameproto.h claimed for three phases that header values were
# percent-encoded, and that was true of the parser and false of the builder --
# nothing ran the two halves against each other.
FRAMECHECK_OBJS = $(addprefix $(BUILD)/,framecheck.o frameproto.o str.o log.o)
framecheck: $(BUILD)/framecheck.exe
$(BUILD)/framecheck.exe: $(FRAMECHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(FRAMECHECK_OBJS)}
	@echo "built $@"

# Asserts the C++ guards actually guard: that Buf reports truncation at and
# past the limit, and that every destructor fires, in order, on every path.
# Guard code that is wrong is worse than no guard, because it is trusted.
CPPCHECK_OBJS = $(addprefix $(BUILD)/,cppcheck.o str.o log.o)
cppcheck: $(BUILD)/cppcheck.exe
$(BUILD)/cppcheck.exe: $(CPPCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(CPPCHECK_OBJS)}
	@echo "built $@"

# 03.12: prints the guest's own capability-enum values by name (stringified
# macros, not retyped), so tests/python/test_guest_caps.py can catch a value
# being RE-MEANT on one side without the other -- something a range check on
# the wire integer alone cannot see.
CAPSCHECK_OBJS = $(addprefix $(BUILD)/,capscheck.o)
capscheck: $(BUILD)/capscheck.exe
$(BUILD)/capscheck.exe: $(CAPSCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(CAPSCHECK_OBJS)}
	@echo "built $@"

# The three answers to a filename collision, run against real directories.
# They differ ONLY in what happens to the two copies that clash, which is
# exactly the kind of difference that reads correct and behaves otherwise:
# `stash` takes the incoming file in one mode and the target's own in the
# other, one character apart at the call site, and swapping them would still
# compile and still report success while putting the wrong version live.
# What tool detection found, in the wire encoding -- one producer for the
# guest and for the tests, so the format Python parses is the format that
# ships. Takes a directory so it can run under Wine against a synthetic tree
# with its own INI, without the toolchains actually being installed.
TOOLCHECK_OBJS = $(addprefix $(BUILD)/,toolcheck.o tooldet.o str.o log.o)
toolcheck: $(BUILD)/toolcheck.exe
$(BUILD)/toolcheck.exe: $(TOOLCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(TOOLCHECK_OBJS)}
	@echo "built $@"

WORKCHECK_OBJS = $(addprefix $(BUILD)/,workcheck.o workcopy.o path.o str.o \
                 log.o sync.o miniz.o)
workcheck: $(BUILD)/workcheck.exe
$(BUILD)/workcheck.exe: $(WORKCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  file {$(WORKCHECK_OBJS)}
	@echo "built $@"

PROBE_OBJS = $(addprefix $(BUILD)/,probe.o str.o miniz.o)
probe: $(BUILD)/probe.exe
$(BUILD)/probe.exe: $(PROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(PROBE_OBJS)}
	@echo "built $@"
	@echo "--- import audit (an NT-only symbol here would kill it at load) ---"
	@objdump -p $@ | grep -i 'DLL Name'
	@objdump -p $@ | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print $$NF}' \
	  | grep -E 'SetHandleInformation|OpenSCManager|CM_Get|GetGuiResources' \
	  && echo "  ^^ NT-ONLY IMPORT -- WILL NOT LOAD ON WIN9X" || echo "  clean"

# `all` builds the CHECK BINARIES TOO, and that is not tidiness.
#
# It used to be retroclaude.exe alone, so every check binary was only ever
# rebuilt by naming its own target. tests/python runs them under Wine, which
# means the suite happily ran an hours-old netcheck.exe against freshly
# changed net.cpp and reported a pass -- the exact failure the standing rule
# about verifying the real thing exists to stop, arriving through the build
# system instead of the platform. Caught when a new --slowsend mode was
# "missing" from a binary that predated it, and the test's own argv parse
# printed the flag as a hostname.
#
# The cost is a few seconds. A green suite that tested the previous commit is
# worth far less than that.
retroclaude: $(BUILD)/retroclaude.exe
retroclaude.exe: $(BUILD)/retroclaude.exe

all: $(BUILD)/retroclaude.exe synccheck inicheck cppcheck framecheck \
     netcheck rpccheck probe htmlspike capscheck workcheck toolcheck

# Headless conformance check: links the REAL net.c/frameproto.c so it exercises
# the same code the GUI does, without needing GUI automation.
# net.o now calls into the RPC worker, so netcheck must link it too or
# the build breaks with unresolved externals.
NETCHECK_OBJS = $(addprefix $(BUILD)/,netcheck.o net.o frameproto.o \
                log.o str.o path.o miniz.o exec.o rpc.o sync.o syncrpc.o)
netcheck: $(BUILD)/netcheck.exe
$(BUILD)/netcheck.exe: $(NETCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(NETCHECK_OBJS)}
	@echo "built $@"

# Headless conformance check for 04.5's lane table: links the REAL rpc.c and
# frameproto.c and drives rc_rpc_route/rc_rpc_submit directly, so this is the
# same worker the GUI runs, not a reimplementation of it. No live bridge
# needed -- see the comment at the top of rpccheck.c for why rc_net_send is a
# safe no-op here.
RPCCHECK_OBJS = $(addprefix $(BUILD)/,rpccheck.o net.o frameproto.o \
                log.o str.o path.o miniz.o exec.o rpc.o sync.o syncrpc.o)
rpccheck: $(BUILD)/rpccheck.exe
$(BUILD)/rpccheck.exe: $(RPCCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ file {$(RPCCHECK_OBJS)}
	@echo "built $@"

# Every object depends on every header. Coarse, but there are seven of them
# and the failure it prevents is vicious: adding two fields to rc_transcript
# did not rebuild main.o, so main.c still allocated the OLD struct while
# transcript.c wrote the new fields past the end of it, corrupting the next
# global. The symptom was PostMessage failing on a valid HWND -- nowhere near
# the change, and it looked exactly like a Win9x message-queue limit.
# Includes are QUALIFIED BY LAYER -- #include "rpc/rpc.h", never "rpc.h" --
# and that is not decoration.
#
# win/rpc/rpc.h collides with the Windows SDK's own rpc.h. While every header
# sat in one directory the collision never showed, because a quoted include
# searches the including file's own directory first and always found the
# sibling. Splitting into layers removed that accident: net.c resolved "rpc.h"
# to the SDK's copy, picked up none of our declarations, and the ONLY symptom
# was a W131 "no prototype found" on a build that still succeeded and ran.
#
# Putting our paths first instead is worse: the SDK's rpcndr.h includes rpc.h
# and would then get ours. Qualifying by layer cannot collide in either
# direction, and it makes a file's dependencies legible at the include site.

# One include path per layer rather than one flat directory, so a header says
# which layer it belongs to by where it lives. The layering is real and was
# already there, just invisible: core (log, ini) is a leaf everything uses;
# net and sync sit above it; rpc routes between them; app is the only thing
# that reaches into four layers, which is what an entry point does.
VPATH = app:net:rpc:sync:core:checks

HDRS = $(wildcard app/*.h net/*.h rpc/*.h sync/*.h core/*.h)

# -fr= puts the compiler's per-file error listing in the build tree. Without
# it Open Watcom drops a .err next to the SOURCE, which is how win/ collected
# frameproto.err and probe.err.
$(BUILD)/%.o: %.c $(HDRS) | $(BUILD)
	@$(WCC) $(CFLAGS) -fo=$@ -fr=$(BUILD)/$*.err $<

# The same flags through the C++ compiler. A module migrates by being renamed;
# nothing else in the tree knows, because every header is extern "C".
$(BUILD)/%.o: %.cpp $(HDRS) | $(BUILD)
	@$(WPP) $(CFLAGS) $(CXXONLY) -fo=$@ -fr=$(BUILD)/$*.err $<

# mshtml.h has a bare `typedef enum _BoolValue {...};` with no name for the
# typedef to bind to -- harmless in C, which is how every other Watcom header
# using this pattern has gone unnoticed, but C++ diagnoses it (W146). It is
# the shipped header, not our code, and `#pragma warning 146 9` does not
# silence it in this Watcom build, so it is disabled here instead, for this
# one object only.
$(BUILD)/htmlview.o: CFLAGS += -wcd=146 -wcd=602

# The resource script. `wrc -r` compiles only -- it does not touch an EXE --
# and the .res is then bound at link time by `option resource=` below. The
# other order (link first, then `wrc file.res file.exe` to inject) also works
# and is what most Watcom examples show; it is not used here because it makes
# the EXE a two-step artefact, and a build interrupted between the steps
# leaves a retroclaude.exe that is newer than every prerequisite and has no
# icon. One link, one output.
#
# -i=$(CURDIR) is what lets gaston.rc say `#include "app/resource.h"` -- the
# same layer-qualified spelling the C++ uses, so the id is defined once for
# both compilers.
# Every binary this script names lives in resources/, not beside the C++.
# The wildcards are deliberate: a new pie slice or a replaced icon must not
# need a Makefile edit to be picked up, and a stale .res that silently keeps
# the old artwork is the failure this dependency list exists to prevent.
$(BUILD)/gaston.res: app/gaston.rc app/resource.h \
                     $(wildcard resources/*.ico) $(wildcard resources/*.bmp) \
                     | $(BUILD)
	@$(WRC) -q -r -bt=nt -i="$(CURDIR)" -fo=$@ app/gaston.rc

$(BUILD)/miniz.o: third_party/miniz/miniz.c third_party/miniz/miniz.h | $(BUILD)
	@$(WCC) $(CFLAGS) -fo=$@ -fr=$(BUILD)/miniz.err third_party/miniz/miniz.c

$(BUILD):
	@mkdir -p $(BUILD)

# The map is not a debugging nicety: `make check` needs symbol names to gate on
# stack frame sizes, and objdump cannot read Watcom's OMF objects.
$(BUILD)/retroclaude.exe: $(OBJS) $(BUILD)/gaston.res
	@$(WLINK) $(LFLAGS), resource=$(BUILD)/gaston.res, \
	  map=$(BUILD)/retroclaude.map $(LIBS) name $@ file {$(OBJS)}
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# The CMOV gate used to be a count against a baseline, and the baseline moved
# three times without a single real instruction behind it: 1 -> 2 -> 1 -> 7.
#
# The mechanism was understood by the second move and is worth keeping, because
# it applies to any linear disassembly of this image. `objdump -d` walks the
# whole AUTO section, and Open Watcom puts read-only data in AUTO beside code,
# so a GUID table or the padding between two library functions decodes as
# whatever its bytes happen to spell. Adding a function slides the data after
# it and the number changes. Resources do NOT move it -- `.rsrc` is READONLY
# DATA and objdump never walks it, which BACKLOG.md 10 predicted wrongly and
# was measured when gaston.res landed.
#
# So the gate locates each hit instead (tools/check/cmovcheck.py): the linker map
# says which module a symbol came from, and only a hit inside an object file
# THIS BUILD COMPILED is something `-5` governs. The current seven are one
# inside uuid.lib's _GUID_FONTBOLD and six in clib3r.lib's
# __DefaultExceptionHandler_ padding, and they are printed every run rather
# than summed into a number nobody can check.
PYTHON ?= python3

# Static PE audit. The import table is the gate that matters: a single NT-only
# symbol kills the EXE at load on Win98, before WinMain, with no log written.
check: $(BUILD)/retroclaude.exe
	@echo "=== identity ==="; file $(BUILD)/retroclaude.exe
	@echo "=== imports (DLL allow-list, NT-only deny-list, W-suffix, PE 4.0) ==="
	@# THIS STEP CAN FAIL, which the three shell pipelines it replaces could
	@# not. They printed the DLL list, printed the W-suffix imports and
	@# printed the OS/subsystem version -- all three exit 0 whatever they
	@# find, so CLAUDE.md's "make check audits imports" was a human reading
	@# scrollback, and under `make all` or in CI a regression went green.
	@#
	@# The W-suffix grep also aimed at the wrong class. MoveFileExA is ANSI,
	@# has no W suffix, lives in KERNEL32 and does not exist on 9x --
	@# win/sync/sync.cpp says in a comment that somebody will want to call it
	@# to make the commit atomic. That edit used to pass every gate here and
	@# then die at load on the guest with no log. See audit 01.29.
	@$(PYTHON) $(TOP)/tools/check/imports.py $(BUILD)/retroclaude.exe
	@echo "=== CMOV (a Pentium MMX has none; 686+ only) ==="
	@# Located, not counted -- see tools/check/cmovcheck.py for why the baseline
	@# this replaces moved three times without anything being wrong.
	@$(PYTHON) $(CURDIR)/../tools/check/cmovcheck.py $(BUILD)/retroclaude.exe \
	   $(BUILD)/retroclaude.map $(OBJS)
	@echo "=== unbounded string functions (the overflow family) ==="
	@# These write until a NUL that may not be there. rc_strcpy/rc_strncpy/
	@# rc_strf replace them and always terminate, so the READING family
	@# (strcmp, strlen, strchr...) can never be handed an unterminated
	@# operand -- which is the property that makes those safe, and it is a
	@# property of the writes, not of the reads.
	@#
	@# \b does not match inside rc_strcpy, because '_' is a word character.
	@# third_party is excluded: miniz is vendored and not ours to rewrite.
	@bad=$$(grep -rnE '\b(strcpy|strcat|sprintf|vsprintf|gets|lstrcpyA?|lstrcatA?|wsprintfA?|_mbscpy|alloca)[[:space:]]*\(' \
	        $(CURDIR) --include='*.c' --include='*.cpp' --include='*.h' \
	        2>/dev/null | grep -v third_party || true); \
	 if [ -n "$$bad" ]; then \
	   echo "$$bad"; \
	   echo "   FAIL: use rc_strcpy/rc_strncpy/rc_strf (win/core/str.h)"; \
	   exit 1; \
	 else echo "   ok: none"; fi
	@echo "=== static constructors (they run BEFORE WinMain, so before the log) ==="
	@# Verified on the target: a file-scope object's constructor runs before
	@# WinMain and its destructor at exit. log.c opens retroclaude.log from
	@# WinMain's first statement deliberately, because "no log file" is the
	@# signal that main never ran -- so a static ctor that faults reproduces
	@# exactly the undebuggable position the logging discipline exists to
	@# avoid. POD statics only.
	@if objdump -t $(BUILD)/retroclaude.exe 2>/dev/null \
	   | grep -qE '__CPP_INIT|__sti__|_STI__'; then \
	  echo "   FAIL: a static constructor is linked in; see win/core/rc_cpp.h"; \
	  exit 1; \
	else echo "   ok: none"; fi
	@echo "=== stack frames (a frame >= one page steps over the guard page) ==="
	@$(PYTHON) $(TOP)/tools/check/stackframes.py $(BUILD)/retroclaude.exe $(BUILD)/retroclaude.map
	@echo "=== static allocation (the guest has 28KB free after a turn) ==="
	@# Uninitialised statics are committed at load and are spent before the
	@# app does anything -- rc_net_send's frame build buffer alone is 66,560
	@# bytes. `objdump -h` reports the RAW section size, which omits BSS
	@# entirely and makes this look 7x smaller than it is, so read the PE
	@# section headers directly.
	@$(PYTHON) -c "import struct, sys;d=open('$(BUILD)/retroclaude.exe','rb').read();\
pe=struct.unpack_from('<I',d,0x3c)[0];\
n=struct.unpack_from('<H',d,pe+6)[0];o=pe+24+struct.unpack_from('<H',d,pe+20)[0];\
rows=[(d[o+i*40:o+i*40+8].rstrip(bytes(1)).decode(),)+struct.unpack_from('<IIII',d,o+i*40+8) for i in range(n)];\
[print('   %-8s image %7d  file %7d  bss %7d' % (r[0],r[1],r[3],max(0,r[1]-r[3]))) for r in rows];\
tot=sum(max(r[1],r[3]) for r in rows);\
print('   total committed at load: %d bytes (cap 450000)' % tot);\
sys.exit(1) if tot > 450000 else None"

clean:
	@rm -rf $(BUILD)
