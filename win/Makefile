# gaston.exe -- the single Win98 guest binary.
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
WLIB  = $(OWBIN)/wlib

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
# -xs: exceptions ON, for the STL (vector, string, wstring), by decision on
# 2026-09-25 after measuring the cost: +27 KB committed at load for the
# switch alone, about +40 KB with the containers in use. The fixed MAX_*
# tables and hand-rolled string building were costing more than that in
# code and in memory held at worst case. Open Watcom's STL does not compile
# without it. operator new throws std::bad_alloc (core/heap.cpp); App::run
# and thread entry points catch it.
# -DNOMINMAX: windows.h's min/max macros break <limits> and friends.
# -wcd=433: "mangled name truncated" fires for ordinary instantiations such
# as vector<string>. Checked: vector<string> and vector<wstring>, both
# truncated, instantiated in two objects and linked into one exe, each ran
# its own code correctly (150 and 350 from a Wine run).
CXXONLY = -xs -wcd=433

CFLAGS = -bt=nt -bm -zq -otexan -5 -wx \
         -DWINVER=0x0400 -D_WIN32_WINNT=0x0400 \
         -DWIN32_LEAN_AND_MEAN -DTDEFL_LESS_MEMORY=1 -DNOMINMAX \
         -i="$(CURDIR)" -i="$(CURDIR)/third_party/miniz" \
         -i="$(WATCOM)/h" -i="$(WATCOM)/h/nt"

LFLAGS = system nt_win option quiet, osversion=4.0

# Memory budget ceiling: fail if committed memory exceeds baseline.
# 399,000 -> 405,000 on 2026-09-25, by decision, for the client moving onto
# libcore's App and IniFile (docs/REMOTE.md C0): measured +6,144 over
# 398,808 with both linked. 405,000 -> 408,000 the same day: the estimate
# left out ClientConfig, the message filters and the three services in
# main.cpp; C4 measured 407,528. The memory pass (BACKLOG.md 2, -os alone
# is -32 KB) comes after the refactor, by decision. 408,000 -> 412,000 at
# C5b (408,060 measured): headroom for the rest of C5, where each module's
# callbacks become an interface with a vtable. The memory pass tightens it
# again. 412,000 -> 450,000 when exceptions went on for the STL (by
# decision, 2026-09-25): the switch measured 434,648 before any container
# is used, and the headroom is for the modules that move onto
# vector/string. 450,000 -> 470,000 as the modules move over (chat list,
# option menus, attachment bar measured 451,081 with app/ at -os): each
# container type instantiates its own code, roughly 5-10 KB per module.
# Raise it only with a measured reason.
#
# 470,000 -> 474,096 on 2026-10-01, by the user's decision, and the reason is
# alignment rather than code. At 470,000 the client was not 2,263 bytes from
# the cap in any useful sense -- it was TWO BYTES from a 4,096-byte cliff.
# DGROUP's data chain ended at 0x469ffe and _BSS is page-aligned at
# 0x46a000, so the next byte of static data anywhere in gaston pushed _BSS to
# 0x46b000 and the gate, which sums max(virtual, raw) per PE section, charged
# the whole empty page. Measured when the `automation` HELLO capability
# landed: the feature itself is 404 bytes of _TEXT and CONST, _BSS and _DATA
# unchanged to the byte, and the reported growth was 4,500. Its own HELLO key
# string is eleven characters, so there was no version of it that fit.
#
# The page is bought once. 474,096 carries that padding page plus the 3,970
# bytes of static data that now fit inside it, so the next small feature does
# not re-enact this. The alternative was hunting 126 bytes of CONST to delete
# somewhere unrelated, which realigns _BSS and buys nothing structural -- the
# feature after it lands on the same boundary and somebody goes hunting again.
#
# 474,096 -> 478,192 on 2026-10-02: the padding page bought above is now spent,
# and this is the next one, for the same structural reason. Two features landed
# on it -- the CPUID brand string (exec/cpuid.cpp, plus a 64-byte and a 16-byte
# field in SystemInfo and a cpu[] buffer grown from 64 to 160) and FSLS, the
# client's directory listing, which is an 8KB SINK ON THE HEAP and whose static
# cost is the handler and its strings. Measured 474,393 against 474,096: 297
# bytes over, which is again the gate charging a whole page rather than the
# features being large. One page, bought once, with room above it so the next
# small feature does not re-enact this either.
#
# WHAT WOULD MAKE THIS A REAL PROBLEM rather than an accounting one: the number
# the guest actually cares about is runtime working set on a 32MB box, and this
# gate cannot see a thread stack at all. If this is hit twice more, measure
# that instead of raising this again.
#
# 478,192 -> 502,768 (six pages) on 2026-10-02, ONCE for a whole feature set
# rather than a page at a time: the `input` step list, its key/layout and
# target resolution, and the fs mutation ops (docs/PROGRESS.md, "One input
# tool"). Its first commit -- the `~=` glob and the ambiguity candidates --
# already crossed the old line by 1,321 bytes. This is the "hit twice more"
# the paragraph above warns about, so the runtime number is measured this
# time and not just this one: free physical memory with the client up,
# before and after the feature set, on the guest
# (docs/measurements/workingset-*-20261002.txt).
#
# 502,768 -> 506,864 (one page), the same day: copy, move and delete of whole
# trees -- the Recycle Bin, a move that never loses its source, nothing
# overwritten unless asked (fs/tree.h) -- measured at 506,233. Written as
# plain data first; the std::string version was 11KB. The runtime number is
# re-measured on the guest with it (docs/measurements/workingset-*).
#
# 506,864 -> 515,056 (two pages), the same day: the `ui` listing of one
# control's items -- what `input`'s `item` targets. About 1.5KB of code and no
# new static data (the bss symbol lists are identical), but it pushed both AUTO
# and DGROUP over a page boundary, and the gate charges whole pages.
# 515,056 -> 536,576 (five pages), 2026-10-04, PROVISIONAL: the Options
# dialog's feature set -- per-client settings, execution modes, the client's
# protected/dangerous/trusted lists and the PREFS body (docs/OPTIONS-PLAN.md).
# Its first commit measured 531,881: about 11KB of code and 4KB of data, all
# of it the feature's own. Already trimmed once: a std::vector<std::string>
# in config.h (included by half the program) cost 25KB more and is a
# TextList now (core/textlist.h). As in 2026-10-02 the runtime number gets
# measured for the whole set when its last client commit lands, against the
# deployed build of 91b6b76 kept as ~/backups/gaston-baseline-91b6b76.exe,
# and this line is then set once to what that justifies. Raised again
# provisionally to 569,344 for the rest of the set: the property sheet's own
# machinery (core/dialogpage, core/dlgtemplate, app/optsheet) measured
# 8.7KB with one page, and five more pages and the lists inspector follow.
# And to 598,016 when the Tools page crossed it at 575,993, with the Misc
# page and the command log still to come.
COMMITTED_CAP ?= 598016

# wsock32 rather than ws2_32: we use the Winsock 1.1 API surface, and
# wsock32.dll is present on every target from Win95 onward (on Win98 it is a
# forwarder to ws2_32). comctl32 is for the status bar.
LIBS   = library wsock32.lib, comctl32.lib, ole32.lib, oleaut32.lib, uuid.lib

# Objects mirror the source tree: app/input.cpp -> $(OBJ)/app/input.o. Every
# object used to land in one flat $(BUILD)/%.o found through VPATH, which
# takes the FIRST match and says nothing -- so two sources with the same name
# in different layers (app/input.cpp, the compose box, and the planned
# ui/input.cpp, docs/REMOTE.md) would have silently built one of them twice.
# With mirrored paths that cannot happen, and VPATH is gone.
#
# Targets list SOURCES, by path, and the objects are derived. A list of bare
# object names hid which layer each came from.
OBJ = $(BUILD)/obj
objs = $(patsubst %,$(OBJ)/%.o,$(basename $(1)))

MINIZ = third_party/miniz/miniz.c

# Static libraries (docs/REMOTE.md). Each is built from its sources' mirrored
# objects into $(LIBDIR), and targets list it among their inputs like any
# object. wlink pulls in only the members something references.
#
# No duplicate-name check is needed inside a lib: wlib names each member by
# the full source path the compiler recorded (`/.../win/core/str.c`), not the
# object's basename, so a/x.o and b/x.o coexist (verified 2026-09-25).
LIBDIR = $(BUILD)/lib
CORE_SRCS = core/log.cpp core/str.c core/path.c core/inifile.cpp core/textlist.cpp \
            core/dlgtemplate.cpp core/dialogpage.cpp \
            core/app.cpp core/window.cpp core/heap.cpp core/thread.cpp
LIBCORE = $(LIBDIR)/libcore.lib
# libwire (docs/REMOTE.md, step 2): the frame codec and Connection, the
# framed TCP link. Knows the wire format and no frame types; each protocol's
# types and lanes live with its exe. Listed BEFORE libcore wherever both are
# linked, because it calls into libcore. Connection compresses with miniz,
# which each exe that links it lists as an object ($(MINIZ)).
WIRE_SRCS = wire/frame.cpp wire/connection.cpp wire/listener.cpp
LIBWIRE = $(LIBDIR)/libwire.lib

# libexec (docs/REMOTE.md step 3, docs/libexec-plan.md): running commands
# and managing processes. libcore only; it knows nothing of the wire, and
# mapping requests to jobs is the exe's job. Listed BEFORE libcore wherever
# both are linked.
EXEC_SRCS = exec/cpuid.cpp exec/execjob.cpp exec/process.cpp exec/system.cpp
LIBEXEC = $(LIBDIR)/libexec.lib

# libfs (docs/REMOTE.md step 5a, docs/libfs-plan.md): paths, stat, the walk,
# the temp/verify/rename write and the chunked transfer primitive. libcore
# only; it knows nothing of the wire, and turning a listing into frames is the
# exe's job. Listed BEFORE libcore wherever both are linked.
#
# drives.cpp and freespace.cpp are separate translation units on purpose: the
# client wants free space for one volume and never enumerates drives, so
# splitting them keeps the enumeration out of gaston.exe's image.
FS_SRCS = fs/path.cpp fs/file.cpp fs/dir.cpp fs/list.cpp fs/reader.cpp \
          fs/writer.cpp fs/xfer.cpp fs/freespace.cpp fs/drives.cpp fs/ops.cpp fs/tree.cpp
LIBFS = $(LIBDIR)/libfs.lib

# Reading and driving the desktop (docs/UIAUTOMATION.md, REMOTE.md step 6).
# libcore only: it knows nothing of the wire, and turning a tree into
# frames is the exe's job. Listed BEFORE libcore wherever both are linked.
AUTOMATION_SRCS = automation/tree.cpp automation/select.cpp \
                  automation/resolve.cpp automation/watch.cpp \
                  automation/wait.cpp automation/calldeadline.cpp \
                  automation/input.cpp automation/msaa.cpp \
                  automation/shot.cpp automation/steps.cpp
LIBAUTOMATION = $(LIBDIR)/libautomation.lib

# A target's inputs are objects, libraries and (for the client) a .res; this
# turns them into wlink directives. `library` may repeat.
LINKIN = file {$(filter %.o,$^)} $(foreach l,$(filter %.lib,$^),library $(l))

CLIENT_SRCS = app/main.cpp app/mainwindow.cpp app/bar.cpp app/chatlist.cpp app/xferstat.cpp \
       app/pagebar.cpp app/optmenu.cpp app/input.cpp app/optsheet.cpp app/listsdlg.cpp \
       app/toolbar.cpp app/export.cpp app/cache.cpp app/workdir.cpp \
       app/workcopy.cpp app/workdlg.cpp app/tooldet.cpp app/pillbar.cpp \
       app/modes.cpp app/protect.cpp \
       app/screenshot.cpp app/statusbar.cpp app/reconnect.cpp app/turn.cpp \
       app/config.cpp app/machineid.cpp app/oleservice.cpp app/connservice.cpp \
       net/net.cpp net/frameproto.cpp app/htmlview.cpp $(MINIZ) \
       rpc/rpc.cpp rpc/procrpc.cpp rpc/fsrpc.cpp sync/manifest.cpp sync/putpath.cpp sync/scanner.cpp sync/verify.cpp \
       sync/syncrpc.cpp \
       ui/uiservice.cpp ui/uirpc.cpp ui/uiengine.cpp ui/uirec.cpp ui/inject.cpp
# libautomation before libwire before libexec before libcore: the first three
# call into libcore, and wlink resolves in the order the libraries are named.
OBJS = $(call objs,$(CLIENT_SRCS)) $(LIBAUTOMATION) $(LIBWIRE) $(LIBEXEC) \
       $(LIBFS) $(LIBCORE)

.PHONY: gremote gupdate fscheck execcheck execprobe keyprobe focusprobe uicheck autocheck hookdll msgids synccheck inicheck idcheck inifilecheck appcheck windowcheck wirecheck cppcheck framecheck all clean check check-sources run netcheck rpccheck probe htmlspike capscheck workcheck toolcheck

# Without this, make's default goal is the FIRST target in the file, which is
# htmlspike -- a Phase 0 spike. `make -C win` therefore printed "Nothing to be
# done for 'htmlspike'" and exited 0 while building nothing, which reads
# exactly like a successful no-op build of the real thing. Same class of trap
# as the stale check binaries described above `all`.
.DEFAULT_GOAL := all

# Spike: a minimal MSHTML host in plain C. Answers whether the transcript can
# move to HTML, which the 512 KB measured cost says is affordable.
HTMLSPIKE_OBJS = $(call objs,checks/htmlspike.c)
htmlspike: $(BUILD)/htmlspike.exe
$(BUILD)/htmlspike.exe: $(HTMLSPIKE_OBJS)
	@$(WLINK) $(LFLAGS) library ole32.lib, oleaut32.lib, uuid.lib \
	  name $@ $(LINKIN)
	@echo "built $@"
	@objdump -p $@ | grep -i 'DLL Name'

# Phase 0 guest experiments. Console subsystem so it can be run from a DOS box
# and its output read without a GUI.
SYNCCHECK_OBJS = $(call objs,checks/synccheck.cpp sync/manifest.cpp sync/putpath.cpp sync/scanner.cpp sync/verify.cpp \
                 $(MINIZ)) $(LIBFS) $(LIBCORE)
synccheck: $(BUILD)/synccheck.exe
$(BUILD)/synccheck.exe: $(SYNCCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# Asserts save() and load() agree on the field list. Two bugs of that exact
# shape have shipped (font_size, then the sync root), and neither was visible
# from reading the code.
INICHECK_OBJS = $(call objs,checks/inicheck.cpp app/config.cpp app/modes.cpp) $(LIBCORE)
inicheck: $(BUILD)/inicheck.exe
$(BUILD)/inicheck.exe: $(INICHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# The machine identity: minted once with CoCreateGuid, written beside the exe,
# read back on every launch after that. Under Wine because the file half is
# real I/O and the mint half is OLE32 -- and because a client that silently
# re-mints on every launch is indistinguishable from one that works until the
# chat list comes up empty. See BACKLOG.md 21.
IDCHECK_OBJS = $(call objs,checks/idcheck.cpp app/machineid.cpp) $(LIBCORE)
idcheck: $(BUILD)/idcheck.exe
$(BUILD)/idcheck.exe: $(IDCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# IniFile's refusals, against INI files it writes itself: a bare filename the
# profile API would read from C:\WINDOWS, an inline comment that becomes part
# of the value, a value one byte too long, an unknown key. See
# docs/REMOTE.md, Configuration.
INIFILECHECK_OBJS = $(call objs,checks/inifilecheck.cpp) $(LIBCORE)
inifilecheck: $(BUILD)/inifilecheck.exe
$(BUILD)/inifilecheck.exe: $(INIFILECHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# App's lifecycle, traced: init and start in order, signal-all-then-join in
# reverse, a stuck join leaked rather than deleted, quit() from another
# thread, WM_ENDSESSION. See docs/REMOTE.md, "Services and lifecycle".
APPCHECK_OBJS = $(call objs,checks/appcheck.cpp) $(LIBCORE)
appcheck: $(BUILD)/appcheck.exe
$(BUILD)/appcheck.exe: $(APPCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# Not a check: measures on the guest what libexec's design depends on --
# Toolhelp, whether a grandchild outlives its killed shell, exit codes through
# COMMAND.COM, socket inheritance, launch's show value. Read back over COM1.
EXECPROBE_OBJS = $(call objs,checks/execprobe.cpp) $(LIBCORE)
execprobe: $(BUILD)/execprobe.exe
$(BUILD)/execprobe.exe: $(EXECPROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# Not a check either: answers the one question BACKLOG 16's fix rests on --
# whether a BACKGROUND process can raise another process's window on 9x.
# Wine cannot answer it, because Wine's foreground rules are the host window
# manager's. Prints to the console; the box it runs on has no serial port.
# keyprobe: how 98 produces a character on a non-US layout -- VkKeyScanEx on
# a loaded layout, ToAsciiEx as a dead-key test, AltGr as Ctrl+Alt, and
# Alt+Numpad -- measured into its own edit control. Wine's layouts are the
# host's, so only the guest answers. Writes KEYPROBE.TXT beside itself.
KEYPROBE_OBJS = $(call objs,checks/keyprobe.cpp) $(LIBAUTOMATION) $(LIBCORE)
keyprobe: $(BUILD)/keyprobe.exe
$(BUILD)/keyprobe.exe: $(KEYPROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

FOCUSPROBE_OBJS = $(call objs,checks/focusprobe.cpp) $(LIBAUTOMATION) $(LIBCORE)
focusprobe: $(BUILD)/focusprobe.exe
$(BUILD)/focusprobe.exe: $(FOCUSPROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# libfs under Wine (docs/libfs-plan.md). Its own check target as well as its
# own binary: fs/list.o and, later, fs/drives.o are gremote-only and the
# client never links them, so without a gate here the CMOV and stack-frame
# checks never see that code at all.
FSCHECK_OBJS = $(call objs,checks/fscheck.cpp) $(LIBFS) $(LIBCORE)
fscheck: $(BUILD)/fscheck.exe
$(BUILD)/fscheck.exe: $(FSCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0, \
	  map=$(BUILD)/fscheck.map $(LIBS) name $@ $(LINKIN)
	@echo "built $@"


# libexec under Wine: the refuse-rather-than-truncate guard, output, the cap,
# timeout and cancel, driven through ExecJob for real.
EXECCHECK_OBJS = $(call objs,checks/execcheck.cpp) $(LIBEXEC) $(LIBCORE)
execcheck: $(BUILD)/execcheck.exe
$(BUILD)/execcheck.exe: $(EXECCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# libwire end to end (wire/connection.cpp, wire/listener.cpp): a Connection
# against one adopted from a Listener over loopback -- round trips, deflate,
# a near-cap frame, order, the idle timeout, the four-call stop. The server
# half has no other user until gremote.
WIRECHECK_OBJS = $(call objs,checks/wirecheck.cpp $(MINIZ)) $(LIBWIRE) $(LIBCORE)
wirecheck: $(BUILD)/wirecheck.exe
$(BUILD)/wirecheck.exe: $(WIRECHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# gremote.exe (docs/REMOTE.md step 4, docs/gremote-plan.md): the Win98 box
# driven from Linux, built from the same libraries the client links. It
# LISTENS, which is the reverse of the client, so libwire's server half
# (Listener, Connection::adopt, the idle timeout) gets its first real user
# here -- wirecheck proved it, but with both ends in one process.
#
# system nt_win, not nt: a console image opens a console when it starts, and
# this is meant to come up from RunServices before anyone logs on. It has no
# window beyond App's hidden one.
#
# $(MINIZ) because Connection's Codec references it even with compression
# off. No $(LIBFS) yet -- an unreferenced library in CHECK_OWNED makes
# cmovcheck fail with "owned module never appears in the map", so each lib
# joins the list in the commit that first calls into it.
REMOTE_SRCS = remote/main.cpp remote/config.cpp remote/allow.cpp \
              remote/server.cpp remote/optable.cpp remote/reply.cpp \
              remote/control.cpp remote/lane.cpp \
              remote/jobops.cpp remote/fsops.cpp remote/uiops.cpp \
              remote/tray.cpp \
              ui/uiengine.cpp ui/uirec.cpp
# libautomation BEFORE libwire before libexec before libcore, the order the
# client's OBJS uses and the one wlink resolves in.
GREMOTE_OBJS = $(call objs,$(REMOTE_SRCS) $(MINIZ)) $(LIBAUTOMATION) \
               $(LIBWIRE) $(LIBEXEC) $(LIBFS) $(LIBCORE)
gremote: $(BUILD)/gremote.exe
gremote.exe: $(BUILD)/gremote.exe
$(BUILD)/gremote.exe: $(GREMOTE_OBJS)
	@$(WLINK) system nt_win option quiet, osversion=4.0, \
	  map=$(BUILD)/gremote.map $(LIBS) name $@ $(LINKIN)
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# gupdate.exe -- the self-updater, and the only program that can replace
# gremote.exe (win/remote/gupdate.cpp says why a second process is needed at
# all). Its own exe rather than a mode of gremote's, because the whole point
# is to be the thing still running when gremote is not.
#
# Deliberately NOT in GREMOTE_OBJS and deliberately one translation unit: it
# links libcore alone -- no libwire, no libexec, no Winsock, no Toolhelp --
# so the program that has to work when everything else has stopped depends on
# as little as possible. 8.3 output name, like every file that has to reach a
# FAT volume on a CD.
#
# system nt_win for the same reason gremote uses it: a console image opens a
# console, and this is started by something that may be running before
# anybody has logged on.
GUPDATE_OBJS = $(call objs,remote/gupdate.cpp) $(LIBCORE)
gupdate: $(BUILD)/gupdate.exe
gupdate.exe: $(BUILD)/gupdate.exe
$(BUILD)/gupdate.exe: $(GUPDATE_OBJS)
	@$(WLINK) system nt_win option quiet, osversion=4.0, \
	  map=$(BUILD)/gupdate.map $(LIBS) name $@ $(LINKIN)
	@echo "built $@ ($$(stat -c%s $@) bytes)"

# Window and SubclassedWindow (core/window.h): WM_CREATE seeing hwnd(),
# destroy through the object and from outside, two instances of one class,
# two subclassed EDITs that do not share state, detach.
WINDOWCHECK_OBJS = $(call objs,checks/windowcheck.cpp) $(LIBCORE)
windowcheck: $(BUILD)/windowcheck.exe
$(BUILD)/windowcheck.exe: $(WINDOWCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# libautomation's selector grammar and tree walker. Links the library, so
# what is tested is what would ship. Wine settles the grammar completely
# (it is string work) and only proves the walk does not crash.
UICHECK_OBJS = $(call objs,checks/uicheck.cpp ui/uirec.cpp ui/uiengine.cpp \
                ui/inject.cpp) \
               $(LIBAUTOMATION) $(LIBWIRE) $(LIBCORE)
uicheck: $(BUILD)/uicheck.exe
$(BUILD)/uicheck.exe: $(UICHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0, \
	  map=$(BUILD)/uicheck.map $(LIBS) name $@ $(LINKIN)
	@echo "built $@"

AUTOCHECK_OBJS = $(call objs,checks/autocheck.cpp) $(LIBAUTOMATION) $(LIBCORE)
autocheck: $(BUILD)/autocheck.exe
$(BUILD)/autocheck.exe: $(AUTOCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0, \
	  map=$(BUILD)/autocheck.map $(LIBS) name $@ $(LINKIN)
	@echo "built $@"

# The only DLL this project builds, and it exists to answer one question:
# can we get code mapped into another program on Win98. `system nt_dll`
# rather than `nt`, and the hook proc is exported by name because
# SetWindowsHookEx needs a procedure address the system can resolve inside
# every process it injects into. 8.3 name: it rides the same CD and kit as
# everything else. See win/inproc/hookdll.c for what it measures.
HOOKDLL_OBJS = $(call objs,inproc/hookdll.c inproc/collect.c)
hookdll: $(BUILD)/HOOKDLL.DLL
$(BUILD)/HOOKDLL.DLL: $(HOOKDLL_OBJS)
	@$(WLINK) system nt_dll option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# The frame header contract, run against the shipping frameproto.o. Exists
# because frameproto.h claimed for three phases that header values were
# percent-encoded, and that was true of the parser and false of the builder --
# nothing ran the two halves against each other.
FRAMECHECK_OBJS = $(call objs,checks/framecheck.cpp net/frameproto.cpp) \
                  $(LIBWIRE) $(LIBCORE)
framecheck: $(BUILD)/framecheck.exe
$(BUILD)/framecheck.exe: $(FRAMECHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# Asserts the C++ guards actually guard: that Buf reports truncation at and
# past the limit, and that every destructor fires, in order, on every path.
# Guard code that is wrong is worse than no guard, because it is trusted.
CPPCHECK_OBJS = $(call objs,checks/cppcheck.cpp) $(LIBCORE)
cppcheck: $(BUILD)/cppcheck.exe
$(BUILD)/cppcheck.exe: $(CPPCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

# 03.12: prints the guest's own capability-enum values by name (stringified
# macros, not retyped), so tests/python/test_guest_caps.py can catch a value
# being RE-MEANT on one side without the other -- something a range check on
# the wire integer alone cannot see.
CAPSCHECK_OBJS = $(call objs,checks/capscheck.c)
capscheck: $(BUILD)/capscheck.exe
$(BUILD)/capscheck.exe: $(CAPSCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
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
TOOLCHECK_OBJS = $(call objs,checks/toolcheck.cpp app/tooldet.cpp) $(LIBCORE)
toolcheck: $(BUILD)/toolcheck.exe
$(BUILD)/toolcheck.exe: $(TOOLCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

WORKCHECK_OBJS = $(call objs,checks/workcheck.cpp app/workcopy.cpp sync/manifest.cpp \
                 sync/putpath.cpp sync/scanner.cpp sync/verify.cpp $(MINIZ)) $(LIBFS) $(LIBCORE)
workcheck: $(BUILD)/workcheck.exe
$(BUILD)/workcheck.exe: $(WORKCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ \
	  $(LINKIN)
	@echo "built $@"

PROBE_OBJS = $(call objs,checks/probe.c $(MINIZ)) $(LIBCORE)
probe: $(BUILD)/probe.exe
$(BUILD)/probe.exe: $(PROBE_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ $(LINKIN)
	@echo "built $@"
	@echo "--- import audit (an NT-only symbol here would kill it at load) ---"
	@objdump -p $@ | grep -i 'DLL Name'
	@objdump -p $@ | grep -E '^\s+[0-9a-f]+\s+<none>' | awk '{print $$NF}' \
	  | grep -E 'SetHandleInformation|OpenSCManager|CM_Get|GetGuiResources' \
	  && echo "  ^^ NT-ONLY IMPORT -- WILL NOT LOAD ON WIN9X" || echo "  clean"

# `all` builds the CHECK BINARIES TOO, and that is not tidiness.
#
# It used to be gaston.exe alone, so every check binary was only ever
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
gaston: $(BUILD)/gaston.exe
gaston.exe: $(BUILD)/gaston.exe

all: msgids $(BUILD)/gaston.exe gremote gupdate synccheck inicheck idcheck inifilecheck appcheck windowcheck wirecheck cppcheck framecheck autocheck hookdll \
     netcheck rpccheck probe execprobe keyprobe focusprobe execcheck uicheck htmlspike capscheck workcheck toolcheck fscheck

# Headless conformance check: links the REAL net.c/frameproto.c so it exercises
# the same code the GUI does, without needing GUI automation.
# net.o now calls into the RPC worker, so netcheck must link it too or
# the build breaks with unresolved externals.
NETCHECK_OBJS = $(call objs,checks/netcheck.c net/net.cpp net/frameproto.cpp \
                $(MINIZ) rpc/rpc.cpp rpc/procrpc.cpp rpc/fsrpc.cpp sync/manifest.cpp \
                sync/putpath.cpp sync/scanner.cpp sync/verify.cpp sync/syncrpc.cpp ui/uirpc.cpp ui/uiengine.cpp ui/uirec.cpp \
                ui/inject.cpp) \
                $(LIBAUTOMATION) $(LIBWIRE) $(LIBEXEC) $(LIBFS) $(LIBCORE)
netcheck: $(BUILD)/netcheck.exe
$(BUILD)/netcheck.exe: $(NETCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ $(LINKIN)
	@echo "built $@"

# Headless conformance check for 04.5's lane table: links the REAL rpc.c and
# frameproto.c and drives rc_rpc_route/rc_rpc_submit directly, so this is the
# same worker the GUI runs, not a reimplementation of it. No live bridge
# needed -- see the comment at the top of rpccheck.c for why rc_net_send is a
# safe no-op here.
RPCCHECK_OBJS = $(call objs,checks/rpccheck.c net/net.cpp net/frameproto.cpp \
                $(MINIZ) rpc/rpc.cpp rpc/procrpc.cpp rpc/fsrpc.cpp sync/manifest.cpp \
                sync/putpath.cpp sync/scanner.cpp sync/verify.cpp sync/syncrpc.cpp ui/uirpc.cpp ui/uiengine.cpp ui/uirec.cpp \
                ui/inject.cpp) \
                $(LIBAUTOMATION) $(LIBWIRE) $(LIBEXEC) $(LIBFS) $(LIBCORE)
rpccheck: $(BUILD)/rpccheck.exe
$(BUILD)/rpccheck.exe: $(RPCCHECK_OBJS)
	@$(WLINK) system nt option quiet, osversion=4.0 $(LIBS) name $@ $(LINKIN)
	@echo "built $@"

# Header dependencies come from the compiler. They used to be "every object
# depends on every header" over a hardcoded directory list, which was right
# about the failure it prevented and wrong about how: adding two fields to
# rc_transcript did not rebuild main.o, so main.c allocated the OLD struct
# while transcript.c wrote past the end of it, corrupting the next global.
# The symptom was PostMessage failing on a valid HWND -- nowhere near the
# change. The wildcard fixed that, but a header in a directory missing from
# the list silently had no dependents at all.
#
# -ad writes a make-style .d beside each object listing exactly the headers it
# included (Watcom omits its own SDK headers). -adfs keeps '/' separators.
# An object with no .d yet has never been built, so it is rebuilt anyway.
#
# The first dependency Watcom writes is the source itself, as a BASENAME
# (`turn.cpp`, not `app/turn.cpp`), which make cannot find; -add= truncates a
# '/' path the same way. FIXDEP deletes that token: the pattern rule already
# names the source, and a .d that names it too breaks the build the day the
# source is renamed (inicheck.c -> inicheck.cpp stopped it with "No rule to
# make target 'checks/inicheck.c'"). The .d keeps only the headers.
DEPFLAGS = -ad=$(@:.o=.d) -adt=$@ -adfs
# ...and dates the .d as its source, never newer than the object: the .d is
# now a prerequisite, and one written after the object would make every
# build rebuild everything.
FIXDEP   = sed -i '1s|: [^ ]*|:|' $(@:.o=.d) && touch -r $< $(@:.o=.d)
-include $(shell find $(OBJ) -name '*.d' 2>/dev/null)

# A header that a .d still names but that has since been deleted or moved
# must not stop the build with "No rule to make target". This rule makes it,
# vacuously, so its dependents rebuild and the compiler reports the real
# problem if there is one.
%.h: ;

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

# -fr= puts the compiler's per-file error listing in the build tree. Without
# it Open Watcom drops a .err next to the SOURCE, which is how win/ collected
# frameproto.err and probe.err.
$(OBJ)/%.o: %.c $(OBJ)/%.d
	@mkdir -p $(@D)
	@$(WCC) $(CFLAGS) $(DEPFLAGS) -fo=$@ -fr=$(@:.o=.err) $<
	@$(FIXDEP)

# The same flags through the C++ compiler. A module migrates by being renamed;
# nothing else in the tree knows, because every header is extern "C".
$(OBJ)/%.o: %.cpp $(OBJ)/%.d
	@mkdir -p $(@D)
	@$(WPP) $(CFLAGS) $(CXXONLY) $(DEPFLAGS) -fo=$@ -fr=$(@:.o=.err) $<
	@$(FIXDEP)

# An object whose .d is MISSING is out of date, not up to date. Without this
# a lost .d left its object stale for good: no header could ever trigger a
# rebuild of it. main.o was built against an older mainwindow.h, allocated
# MainWindow at the old size, and mainwindow.o's new members ran past the
# end of the block -- the client crashed on start writing an HWND through a
# pointer made of someone else's heap data. The empty rule makes a missing
# .d "remade", which makes its object rebuild and write a fresh one.
# .NOTINTERMEDIATE (GNU make 4.4) because a pattern-made prerequisite is
# otherwise "intermediate", and a missing intermediate never makes its
# target out of date -- which is the whole failure this exists to catch.
$(OBJ)/%.d: ;
.NOTINTERMEDIATE: $(OBJ)/%.d

# mshtml.h has a bare `typedef enum _BoolValue {...};` with no name for the
# typedef to bind to -- harmless in C, which is how every other Watcom header
# using this pattern has gone unnoticed, but C++ diagnoses it (W146). It is
# the shipped header, not our code, and `#pragma warning 146 9` does not
# silence it in this Watcom build, so it is disabled here instead, for this
# one object only.
$(OBJ)/app/htmlview.o: CFLAGS += -wcd=146 -wcd=602

# app/ is compiled for SIZE. It is UI code: window procedures, menus,
# layout, dialogs. The expensive work it triggers happens elsewhere -- in
# MSHTML, in the disk and network I/O -- so -otexan's speed buys nothing
# measurable there, and it cost a lot of space: the message handler alone
# was 9 KB larger at -ot once it became a member function, and each STL
# container instantiated in app/ (vector<Row>, vector<string>, ...) comes
# out much bigger. Measured with the chat list and option menus on the STL:
# 458,249 bytes committed at load at -otexan, 438,793 at -os. core/, net/,
# rpc/ and sync/ keep -otexan: the codec, the CRC pass and the transfer
# loops are where speed shows. The whole-client question is BACKLOG.md 2.
$(OBJ)/app/%.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# fs/ is mostly not a loop: refusals, one-shot syscalls, a directory
# enumeration whose cost is the platter, and xfer's per-chunk bookkeeping.
# fs/reader.o is the one that actually runs over every byte, and it keeps
# -otexan. Measured: xfer at -os and at -otexan came to the same committed
# total to the byte, so it is here for honesty rather than for the bytes.
$(OBJ)/fs/path.o $(OBJ)/fs/file.o $(OBJ)/fs/dir.o $(OBJ)/fs/list.o \
$(OBJ)/fs/writer.o $(OBJ)/fs/xfer.o $(OBJ)/fs/freespace.o \
$(OBJ)/fs/drives.o $(OBJ)/fs/ops.o $(OBJ)/fs/tree.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# The `input` step list and the code it drives, for the same reason as
# app/: a step is a resolve, a syscall and a refusal, and its cost is the
# target program's -- never a loop of ours over bytes. Measured 2026-10-02:
# steps.o was 22.5 KB of code at -otexan with its helpers inlined into one
# 6.7 KB `run`. select.o and the walker keep -otexan; the matcher runs over
# every node of every scan.
$(OBJ)/automation/steps.o $(OBJ)/automation/input.o $(OBJ)/ui/uiengine.o \
$(OBJ)/ui/uirpc.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# And the same now applies to most of sync/. The note above says sync/ keeps
# -otexan for "the codec, the CRC pass and the transfer loops"; after step 5a
# the CRC pass is sync/scanner.cpp ALONE, and the rest of the directory is
# frame handling, refusals and adapters onto libfs. So scanner.cpp keeps the
# speed and its neighbours stop paying for it.
$(OBJ)/sync/manifest.o $(OBJ)/sync/putpath.o $(OBJ)/sync/syncrpc.o \
$(OBJ)/sync/verify.o $(OBJ)/rpc/fsrpc.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# remote/ is dispatch, refusals, a handshake and config parsing -- the same
# profile the fs/ and sync/ overrides above were measured for, and none of it
# is a loop. gremote's own budget is what this protects.
$(OBJ)/remote/%.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# core/thread.o: start(), join() and an entry point that catches bad_alloc.
# Three functions, none of them a loop, and the class exists to SAVE memory
# -- so it should not cost image to save memory. Measured on the client:
# 462617 before the class existed, 465177 with it at -otexan, 464665 at -os.
# 512 bytes back, and the remaining 2048 is what one implementation of the
# _beginthreadex rules and the bad_alloc catch costs, paid once.
$(OBJ)/core/thread.o: CFLAGS := $(subst -otexan,-os,$(CFLAGS))

# The resource script. `wrc -r` compiles only -- it does not touch an EXE --
# and the .res is then bound at link time by `option resource=` below. The
# other order (link first, then `wrc file.res file.exe` to inject) also works
# and is what most Watcom examples show; it is not used here because it makes
# the EXE a two-step artefact, and a build interrupted between the steps
# leaves a gaston.exe that is newer than every prerequisite and has no
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

$(BUILD):
	@mkdir -p $(BUILD)

$(LIBCORE): $(call objs,$(CORE_SRCS))
	@mkdir -p $(@D)
	@$(WLIB) -q -b -n $@ $(addprefix +,$^)

$(LIBWIRE): $(call objs,$(WIRE_SRCS))
	@mkdir -p $(@D)
	@$(WLIB) -q -b -n $@ $(addprefix +,$^)

$(LIBEXEC): $(call objs,$(EXEC_SRCS))
	@mkdir -p $(@D)
	@$(WLIB) -q -b -n $@ $(addprefix +,$^)

$(LIBFS): $(call objs,$(FS_SRCS))
	@mkdir -p $(@D)
	@$(WLIB) -q -b -n $@ $(addprefix +,$^)

$(LIBAUTOMATION): $(call objs,$(AUTOMATION_SRCS))
	@mkdir -p $(@D)
	@$(WLIB) -q -b -n $@ $(addprefix +,$^)

# The map is not a debugging nicety: `make check` needs symbol names to gate on
# stack frame sizes, and objdump cannot read Watcom's OMF objects.
# Private window messages must not share a number (tools/check/msgids.py).
# Part of the BUILD, not only `make check`: a clash compiles cleanly and
# misroutes messages at runtime, so it is a build error. Order-only, so it
# runs before every link of the client without forcing one.
msgids:
	@$(PYTHON) $(TOP)/tools/check/msgids.py $(CURDIR)

$(BUILD)/gaston.exe: $(OBJS) $(BUILD)/gaston.res | msgids
	@$(WLINK) $(LFLAGS), resource=$(BUILD)/gaston.res, \
	  map=$(BUILD)/gaston.map $(LIBS) name $@ $(LINKIN)
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
# `make check` runs the per-exe gates for every shipping exe, plus the
# source-tree checks once. Each exe declares what it OWNS -- the objects and
# our own static libs it links, which the CMOV and stack-frame gates must
# treat as ours -- and its committed-at-load budget. gremote.exe joins with
# two more lines (docs/REMOTE.md).
#
# check-<exe> is deliberately not .PHONY: a phony target skips implicit rule
# search, and the gates live in the check-% pattern rule below.
# autocheck joins because the stack-frame gate would have caught a 64KB
# local in it and never got the chance -- that crashed the real machine with
# an invalid page fault while passing every gate here and every test under
# Wine, where the stack is committed up front and there is no guard page to
# jump over. A check exe that links libautomation is exactly the thing that
# needs auditing, since what it links is what ships.
#
# Its committed-at-load budget is generous on purpose. That number is a
# claim about the shipping client's footprint on a 32MB guest; a test
# program is not competing for that memory and never runs beside the client.
# It is set rather than omitted because the gate compares against whatever
# it is given, and 0 means "must be empty", not "not applicable".
check: check-sources check-gaston check-gremote check-gupdate \
       check-autocheck check-fscheck

# gremote's budget is its own, and unlike the client's it is a claim about a
# program that runs BESIDE the thing being measured -- every number taken
# while it is up has this in it. Measured as the lanes landed: 203337 for the
# skeleton, 205897 with the op table and the control lane, 217161 with the
# job lane, 222281 with LS/STAT/DRIVES, 238665 with the rest of fs and
# ship(). The cap is the last of those plus room for the ui lane's handlers
# at step 6; a raise past that wants a measured reason, like the client's.
#
# 250000 -> 264000 on 2026-10-01, by decision, because that single pot of
# headroom was spent twice over on the same day: the tray icon measured
# +7680 (of which +2144 is the -xs exception machinery for a new translation
# unit, before any of its own code) and the UPDATE op measured +2048, which
# together come to 249929 -- 71 bytes under, which is a coincidence and not
# a margin. 264000 is that 249929 plus back the ~10KB the line above
# earmarked for the ui lane, so step 6 still has what it was promised.
#
# With both landed it measures 250441, 512 over that estimate -- PE section
# rounding, which is why the estimate was never going to be exact.
#
# 264000 -> 372000 on 2026-10-03, by decision, for the ui lane (REMOTE.md
# step 6). The earmark above was ~10KB and the lane measures 359609 -- about
# ten times it -- and the reason is not the lane's own code. gremote links
# libautomation now: the tree walker, the selector grammar and resolver, MSAA,
# the event feed, the call deadline, the input layer, the screenshot and the
# step runner, 96,768 bytes of object code before any of win/remote/'s own.
# Measured, not estimated:
#
#     before the lane                  260681
#     + libautomation and win/ui/      359609      AUTO 274286, DGROUP 65721
#
# 372000 is that plus three pages, which is what the remaining work wants
# (none of it is C++ on this side) and no more.
#
# THE REASON THIS NUMBER WAS KEPT TIGHT STILL HOLDS, and raising it does not
# retire it: gremote runs BESIDE the client being measured, so its footprint
# is inside every number this project has ever taken of the client on real
# hardware, and those numbers were taken with a 250KB gremote. The user's
# call was "just raise it, we'll have a separate round of optimizations" --
# that round is BACKLOG 29, which lists what to look at and in what order.
#
# Worth knowing what this number is and is not. It is the sum of the PE
# image's sections: the memory Windows commits when it loads the file, on a
# guest that may have 32MB. It is not a limit on transferring the file, and
# nothing refuses to ship a larger one. It stays tight rather than being
# dropped because gremote runs BESIDE the client being measured, so its
# footprint is inside every number this project has ever taken of the client
# on real hardware.
check-gremote: CHECK_OWNED = $(GREMOTE_OBJS)
check-gremote: CHECK_CAP   = 372000
# gupdate's budget. It links libcore and nothing else, and it runs for a
# few seconds beside a gremote that is on its way out -- so this is a cap on
# a transient, not on a resident, and it is still set tight because the
# whole argument for a separate exe is that it depends on as little as
# possible. Measured 50,688 (2026-10-01); the cap is that plus room for the
# connect-back probe the updater does not do yet (win/remote/gupdate.cpp,
# "what it does not catch"), which would bring in wsock32.
check-gupdate: CHECK_OWNED = $(GUPDATE_OBJS)
check-gupdate: CHECK_CAP   = 64000

check-fscheck: CHECK_OWNED = $(FSCHECK_OBJS)
check-fscheck: CHECK_CAP   = 200000
check-autocheck: CHECK_OWNED = $(AUTOCHECK_OBJS)
# 200,000 -> 212,000 (2026-10-02): the step runner's cases link the runner
# and libautomation's new input primitives -- 203,633 measured. A test
# harness that never ships; the cap is here so it notices growth, not to
# hold it to the client's budget.
check-autocheck: CHECK_CAP   = 212000

check-gaston: CHECK_OWNED = $(OBJS)
check-gaston: CHECK_CAP   = $(COMMITTED_CAP)

check-sources:
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

check-%: $(BUILD)/%.exe
	@echo "=== identity ==="; file $(BUILD)/$*.exe
	@echo "=== imports (DLL allow-list, NT-only deny-list, W-suffix, PE 4.0) ==="
	@# THIS STEP CAN FAIL, which the three shell pipelines it replaces could
	@# not. They printed the DLL list, printed the W-suffix imports and
	@# printed the OS/subsystem version -- all three exit 0 whatever they
	@# find, so CLAUDE.md's "make check audits imports" was a human reading
	@# scrollback, and under `make all` or in CI a regression went green.
	@#
	@# The W-suffix grep also aimed at the wrong class. MoveFileExA is ANSI,
	@# has no W suffix, lives in KERNEL32 and does not exist on 9x --
	@# win/fs/writer.h says in a comment that somebody will want to call it
	@# to make the commit atomic. That edit used to pass every gate here and
	@# then die at load on the guest with no log. See audit 01.29.
	@$(PYTHON) $(TOP)/tools/check/imports.py $(BUILD)/$*.exe
	@echo "=== CMOV (a Pentium MMX has none; 686+ only) ==="
	@# Located, not counted -- see tools/check/cmovcheck.py for why the baseline
	@# this replaces moved three times without anything being wrong.
	@$(PYTHON) $(CURDIR)/../tools/check/cmovcheck.py $(BUILD)/$*.exe \
	   $(BUILD)/$*.map $(CHECK_OWNED)
	@echo "=== static constructors (they run BEFORE WinMain, so before the log) ==="
	@# Verified on the target: a file-scope object's constructor runs before
	@# WinMain and its destructor at exit. log.cpp opens <program>.log from
	@# WinMain's first statement deliberately, because "no log file" is the
	@# signal that main never ran -- so a static ctor that faults reproduces
	@# exactly the undebuggable position the logging discipline exists to
	@# avoid. POD statics only.
	@if objdump -t $(BUILD)/$*.exe 2>/dev/null \
	   | grep -qE '__CPP_INIT|__sti__|_STI__'; then \
	  echo "   FAIL: a static constructor is linked in; see win/core/rc_cpp.h"; \
	  exit 1; \
	else echo "   ok: none"; fi
	@echo "=== stack frames (a frame >= one page steps over the guard page) ==="
	@$(PYTHON) $(TOP)/tools/check/stackframes.py $(BUILD)/$*.exe $(BUILD)/$*.map \
	   $(filter %.lib,$(CHECK_OWNED))
	@echo "=== static allocation (the guest has 28KB free after a turn) ==="
	@# Uninitialised statics are committed at load and are spent before the
	@# app does anything -- rc_net_send's frame build buffer alone is 66,560
	@# bytes. `objdump -h` reports the RAW section size, which omits BSS
	@# entirely and makes this look 7x smaller than it is, so read the PE
	@$(PYTHON) -c "import struct, sys;d=open('$(BUILD)/$*.exe','rb').read();\
pe=struct.unpack_from('<I',d,0x3c)[0];\
n=struct.unpack_from('<H',d,pe+6)[0];o=pe+24+struct.unpack_from('<H',d,pe+20)[0];\
rows=[(d[o+i*40:o+i*40+8].rstrip(bytes(1)).decode(),)+struct.unpack_from('<IIII',d,o+i*40+8) for i in range(n)];\
[print('   %-8s image %7d  file %7d  bss %7d' % (r[0],r[1],r[3],max(0,r[1]-r[3]))) for r in rows];\
tot=sum(max(r[1],r[3]) for r in rows);\
print('   total committed at load: %d bytes (budget $(CHECK_CAP))' % tot);\
sys.exit(1) if tot > $(CHECK_CAP) else None"

clean:
	@rm -rf $(BUILD)
