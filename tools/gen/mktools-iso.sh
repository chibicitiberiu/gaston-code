#!/usr/bin/env bash
# Build a CD image carrying the Gaston Code binaries and the Phase 0 test
# programs into the guest.
#
# A CD rather than a floppy because the binaries already exceed 1.44MB, and
# rather than the network because networking is itself one of the things
# being tested -- this has to work before the guest can talk to the bridge.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
HERE="$REPO"
STAGE="$HERE/vm/tools-stage"
OUT="$HERE/vm/tools.iso"

rm -rf "$STAGE"; mkdir -p "$STAGE"

# Every copy below used to end in `2>/dev/null || true`, and one of them had
# been pointing at $HERE/win/htmlspike.exe since the build moved to
# build/win/. The ISO shipped without HTMLSPK.EXE for as long as that was
# true, and said nothing, because a silenced copy of a file that is not there
# looks exactly like a copy of a file that is not wanted.
#
# So: `need` for anything the guest cannot do without, `want` for the rest --
# and `want` still SAYS when it skips. Only GASTON.INI is genuinely allowed
# to be absent twice, because the two sources deliberately overwrite.
missing=0
need() {                                # fail loudly
  cp "$1" "$STAGE/$2" || { echo "mktools-iso: required: $1" >&2; exit 1; }
}
want() {                                # skip, but out loud
  if [ -e "$1" ]; then cp "$1" "$STAGE/$2"
  else echo "mktools-iso: not built, leaving out of the ISO: $1" >&2
       missing=$((missing + 1)); fi
}

# 8.3 names throughout: the guest may see this via a driver that does not
# surface long filenames, and a truncated name is a confusing failure.
want "$HERE/build/win/probe.exe" "PROBE.EXE"
# stripprobe (ROADMAP stage 4b): the four things the driving indicator
# guesses at, every one of them about what the 9x window manager does with
# a topmost window belonging to somebody else. Wine answers none of them.
want "$HERE/build/win/stripprobe.exe" "STRIPPRB.EXE"
want "$HERE/build/win/htmlspike.exe" "HTMLSPK.EXE"
want "$HERE/build/win/execprobe.exe" "EXECPRB.EXE"
need "$HERE/build/win/gaston.exe" "GASTON.EXE"
want "$HERE/build/win/netcheck.exe" "NETCHECK.EXE"
# The collision-mode check. It runs green under Wine and that proves the
# code, never the platform -- CopyFile, MoveFile and the 8.3 aliasing
# underneath them are exactly the layer Wine reimplements.
want "$HERE/build/win/workcheck.exe" "WORKCHK.EXE"
# Tool detection, printing exactly the bytes HELLO carries. On the guest it
# answers two things Wine cannot: how long a real FAT scan takes, and whether
# a path on a drive with no disk in it raises the system-modal "There is no
# disk in drive A:" that would freeze the app's message pump.
want "$HERE/build/win/toolcheck.exe" "TOOLCHK.EXE"
want "$HERE/build/win/synccheck.exe" "SYNCCHK.EXE"
want "$HERE/build/win/capscheck.exe" "CAPSCHK.EXE"
want "$HERE/build/win/rpccheck.exe"  "RPCCHK.EXE"
# libfs (step 5a). Its Wine result proves the arithmetic and prints the
# platform half rather than asserting it -- read-only semantics, a share-deny
# lock, and which drives the machine actually has -- so the guest run is where
# those get an answer. It is also the only binary that links fs/drives.o and
# fs/list.o at all: the client does not.
want "$HERE/build/win/fscheck.exe"   "FSCHECK.EXE"
want "$HERE/build/win/framecheck.exe" "FRAMECHK.EXE"
# gremote.exe (REMOTE.md step 4): the machine driven from Linux. Unlike
# everything else on this CD it is not a check that prints and exits -- it
# LISTENS, and the host reaches it through QMP hostfwd_add (see
# tools/vm/qmp.py). It is also the first thing here that needs an ini of its
# own, which SETUP.BAT below copies by name.
want "$HERE/build/win/gremote.exe" "GREMOTE.EXE"
# The self-updater, and the only program that can replace GREMOTE.EXE: a
# running exe is locked and gremote's own pid is on the KILL deny list, so
# ship() can deploy a new build to everything on the box except the thing
# doing the deploying (win/remote/gupdate.cpp). UPDATE REFUSES when this
# file is not beside the server, so a CD without it means self-update is
# loudly unavailable rather than quietly broken. SETUP.BAT needs no line
# of its own: `COPY /Y D:\*.EXE` below takes it.
want "$HERE/build/win/gupdate.exe" "GUPDATE.EXE"
# The automation library's own checks, and the wire record's. These carry
# everything Phase 6 added, and three of their answers are known to differ
# under Wine: its oleacc has no Win32 class proxies, it never emits
# EVENT_OBJECT_DESTROY for a hidden popup, and it honours
# WINEVENT_SKIPOWNPROCESS in name only. So these two are exactly the
# binaries whose Wine result proves the code and not the platform.
want "$HERE/build/win/autocheck.exe" "AUTOCHK.EXE"
want "$HERE/build/win/uicheck.exe" "UICHECK.EXE"
# The injection test DLL (probe 0.15d). It must be a real file the system
# can map into other processes, so it rides here like the exes do.
want "$HERE/build/win/HOOKDLL.DLL" "HOOKDLL.DLL"
# vm/gaston.ini points at 10.0.2.2 (the QEMU user-mode host) and wins over
# the Wine-tier default of 127.0.0.1 when it exists.
cp "$HERE/win/gaston.ini" "$STAGE/GASTON.INI" 2>/dev/null || true
cp "$HERE/vm/gaston.ini"      "$STAGE/GASTON.INI" 2>/dev/null || true

# gremote's ini. bind is 0.0.0.0 because QEMU's slirp forwards to the guest's
# own address, not to its loopback -- a server bound to 127.0.0.1 inside the
# guest is not reachable through hostfwd. vm/gremote.ini overrides this if it
# exists, the same way vm/gaston.ini overrides the client's.
cat > "$STAGE/GREMOTE.INI" <<'INI'
[server]
bind=0.0.0.0
port=7401
secret=vm-secret
allow=
heartbeat_timeout_s=30
[exec]
max_timeout_ms=120000
max_output_kb=1024
; The desktop lane (docs/REMOTE.md step 6). This section is written out in
; full on purpose: the default is permissive, and a box that can be driven
; should say so on its own disk rather than only in a document. gremote
; never adds a missing key to an ini that already exists, so what is here is
; what a fresh install gets and what somebody with EDIT.COM can change.
[ui]
; 2 the whole lane (TREE, SHOT, RUN); 1 read-only -- trees and screenshots,
; no input; 0 off, every ui request refused with automation_off on the NEXT
; request rather than the next connection.
automation=2
; 0 skips oleacc.dll even if it is present; the walk falls back to the
; window tree and HELLO says msaa=0.
msaa=1
; Ceilings a request is clamped to, not fixed values.
max_tree_nodes=4000
call_timeout_ms=2000
script_ms=600000
step_ms=120000
INI
sed -i 's/$/\r/' "$STAGE/GREMOTE.INI"
cp "$HERE/vm/gremote.ini" "$STAGE/GREMOTE.INI" 2>/dev/null || true

# Autostart. RunServices runs before anyone logs on, which with
# RegisterServiceProcess (resolved in win/remote/main.cpp) is what makes a
# box in another room reachable after a reboot rather than after somebody
# walks over and logs in.
#
# A .REG applied with `regedit /s` rather than registry calls in the exe:
# ADVAPI32 is not on tools/check/imports.py's allow-list, so writing the key
# from C would fail `make check`. SETUP.BAT is where it is applied, and it is
# NOT applied automatically -- see the comment there.
cat > "$STAGE/GREMOTE.REG" <<'REG'
REGEDIT4

[HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\RunServices]
"GastonRemote"="C:\\GASTON\\GREMOTE.EXE"
REG
sed -i 's/$/\r/' "$STAGE/GREMOTE.REG"
# What to look for on the machine. Without this on the CD the detector finds
# nothing and the feature is inert -- it fails silently, because "no INI" and
# "nothing installed" produce the same empty report by design.
need "$HERE/win/tools_detect.ini" "TOOLSDET.INI"

# The transcript's cold-start document, GENERATED from the bridge's own
# stylesheet so there is exactly one source of truth.
#
# The guest needs a document before it has a connection -- otherwise the
# transcript area is bare grey until a bridge answers, and anything the app
# wants to say in the meantime ("configure a host") has nowhere to appear.
#
# UNTIL STAGE 3 this was GENERATED here, from bridge/view/html.py, and the
# reason was a good one: "two copies of a stylesheet drift", which is the
# bug rc_html::user carries a long comment about -- the guest stripped
# whitespace differently from the bridge and one message rendered two ways.
#
# That argument was right while the bridge was authoritative and it stops
# applying the moment the client is. After stage 3 C4 the bridge has no
# stylesheet to generate from, so the three files below ARE the appearance:
# the client composes view.htm at boot from view.css, and renders every
# block through view.tpl.
#
# The generator's warn-and-continue is gone with it, and that is not just
# tidying: with no DOC frame to fall back on, a failed generation would
# have shipped a kit whose transcript was permanently blank. `need`, so a
# missing file stops the build here instead of on the guest.
#
# CRLF-converted, like every other text file the kit ships. `need` is a
# bare cp, and an LF file into a Win9x profile parser is not a risk worth
# taking for the sake of three sed lines -- view.tpl in particular is read
# by GetPrivateProfileStringA, which is the one API this tree has already
# been burned by (win/core/inifile.cpp:50-107).
# The transcript's spinner. A real file because IE5 animates GIFs but has no
# data: URI support, so it cannot be inlined into the document.
need "$HERE/win/resources/spin.gif" "SPIN.GIF"
need "$HERE/win/resources/view.css" "VIEW.CSS"
need "$HERE/win/resources/export.css" "EXPORT.CSS"
need "$HERE/win/resources/view.tpl" "VIEW.TPL"
sed -i 's/$/\r/' "$STAGE/VIEW.CSS" "$STAGE/EXPORT.CSS" "$STAGE/VIEW.TPL"

# PROBE SCAFFOLDING (Phase 6 gate 0.13c) -- delete with the probe section.
#
# A page with one of everything MSAA might expose through MSHTML: headings,
# body text, links, an edit, checked and unchecked boxes, radios, a select,
# an enabled and a disabled button, a table and a list. Opened in IE on the
# guest so `probe msaa` can walk it and say whether IE's DOM reaches MSAA at
# all -- which decides whether a browser is readable as a control tree or
# only as pixels.
#
# It rides the CD rather than a host HTTP server on purpose: serving it over
# the network would expose a local service to every interface to move one
# 900-byte file, and the CD is already the channel everything else uses.
need "$HERE/tools/gen/w98kit/UITEST.HTM" "UITEST.HTM"

# One command to type blind through QMP send-key, rather than six.
cat > "$STAGE/SETUP.BAT" <<'BAT'
@ECHO OFF
REM COMMAND.COM rejects a trailing backslash on a COPY destination
REM ("Invalid directory"), so C:\GASTON is written without one below.
IF NOT EXIST C:\GASTON\NUL MD C:\GASTON
COPY /Y D:\*.EXE C:\GASTON > NUL
REM HOOKDLL.DLL is NOT an .EXE and so was not copied by the line above --
REM for as long as this file has existed. win/ui/inject.cpp loads it BY NAME
REM from beside the exe, so on every machine installed from this CD the
REM in-process tier answered `no_dll` and there was nothing on disk to say
REM why. Found 2026-10-10 by a probe that could not run gate 0.15d on a guest
REM that had been installed properly. mkw98kit.sh has refused to build a kit
REM without this DLL since the day it was written, with the reason spelled
REM out -- an inert hook looks exactly like "injection does not work on this
REM machine", which is the wrong conclusion -- and the ISO never had the
REM matching line.
IF EXIST D:\HOOKDLL.DLL COPY /Y D:\HOOKDLL.DLL C:\GASTON > NUL
REM gaston.ini is an 8.3 name, so the CD's GASTON.INI IS the file the app
REM opens. Before the rename the app read retroclaude.ini, and a plain copy of
REM the CD's RETROCL.INI once left it on compiled-in defaults with an empty
REM sync root and no error anywhere -- the long names below are that lesson.
REM /Y is inert here -- under COMMAND.COM /c, COPY overwrites silently -- and
REM is kept for whoever types the line by hand, where it stops the
REM "Overwrite (Yes/No/All)?" prompt. Measured on the guest 2026-09-27: the
REM destination's form (directory, named file, *.*) makes no difference to
REM overwriting. A copy that seems to keep an old file right after a CD swap
REM is Win98 still reading the previous disc: re-copy and check contents.
COPY /Y D:\GASTON.INI C:\GASTON\gaston.ini > NUL
REM The transcript's appearance: the stylesheet, the export-only rules and
REM the markup templates. The client composes view.htm from view.css at
REM boot; without them it shows a bare grey rectangle until a bridge
REM answers, which is also when it most needs to say something. Long names
REM deliberately: the client opens view.css, export.css and view.tpl beside
REM itself, and ISO level 1 can only carry the 8.3 spellings.
REM Long name deliberately, for the reason given above: the
REM detector opens tools_detect.ini beside the exe, and ISO level 1 can only
REM carry TOOLSDET.INI. TOOLS.INI is the user's and is NEVER shipped -- it is
REM read second so their additions win, and copying one over it on every
REM install would throw those away.
REM The INIs below are each named rather than copied by wildcard, because
REM the CD carries 8.3 names and some of these want a different one on
REM disk: TOOLSDET.INI becomes tools_detect.ini, which is 13.3 and has no
REM 8.3 spelling at all.
REM
REM gremote.ini is NOT one of those. It is 7.3, a perfectly good 8.3 name,
REM and FAT ignores case -- so GREMOTE.INI and gremote.ini are one file and
REM this line could have used either. It is written lowercase to match what
REM the server opens and for no stronger reason than that. (An earlier
REM comment here claimed the long name was required; that was the
REM retroclaude.ini rule, which applied because that name is 11 characters,
REM and it never applied to this one.)
REM
REM /Y on all of these for consistency with the lines above, and not for
REM any effect it has here: MEASURED on the guest, COMMAND.COM's COPY
REM overwrites silently in a batch file whatever you pass it, and the
REM Yes/No/All prompt /Y suppresses appears only when a person types the
REM line at the prompt. So /Y is for whoever runs these by hand. The
REM destination form does not affect overwriting either -- a directory, a
REM wildcard source and C:\DIR\*.* all replace an existing file, measured.
IF EXIST D:\GREMOTE.INI COPY /Y D:\GREMOTE.INI C:\GASTON\gremote.ini > NUL
IF EXIST D:\GREMOTE.REG COPY /Y D:\GREMOTE.REG C:\GASTON\GREMOTE.REG > NUL
IF EXIST D:\TOOLSDET.INI COPY /Y D:\TOOLSDET.INI C:\GASTON\tools_detect.ini > NUL
IF EXIST D:\VIEW.CSS COPY /Y D:\VIEW.CSS C:\GASTON\view.css > NUL
IF EXIST D:\EXPORT.CSS COPY /Y D:\EXPORT.CSS C:\GASTON\export.css > NUL
IF EXIST D:\VIEW.TPL COPY /Y D:\VIEW.TPL C:\GASTON\view.tpl > NUL
IF EXIST D:\SPIN.GIF COPY /Y D:\SPIN.GIF C:\GASTON\spin.gif > NUL
C:
CD \GASTON
ECHO Copied to C:\GASTON.
ECHO.
ECHO To start GREMOTE at every boot, before anyone logs on:
ECHO     REGEDIT /S C:\GASTON\GREMOTE.REG
ECHO Deliberately not automatic: it opens a port on this machine.  Run PROBE to start the Phase 0 experiments.
BAT
sed -i 's/$/\r/' "$STAGE/SETUP.BAT"

cat > "$STAGE/README.TXT" <<'TXT'
Gaston Code test payload
========================

SETUP.BAT     copies everything to C:\GASTON and cd's there.

STRIPPRB.EXE  stage 4b: HTTRANSPARENT across a process boundary, the gap
              between hiding a window and the pixels under it being right,
              RegisterHotKey for Ctrl+Alt+Shift+F12, and whether
              SW_SHOWNOACTIVATE really keeps a topmost popup out of the
              foreground. Writes STRIPPRB.TXT beside itself.
PROBE.EXE     the Phase 0 experiments: W-suffix imports, threads under -bm,
              16-bit EXEC capture, BI_RLE8, deflate throughput. Writes
              PROBE.LOG beside itself. "No PROBE.LOG" is itself the result:
              it means the image died at load.

HELLO98.EXE   minimal Win32 GUI app. If this does not start, the Open Watcom
              CRT's GetCommandLineW / GetModuleFileNameW imports are the
              reason, and that invalidates the whole toolchain choice.

NETCHECK.EXE  headless framing + networking check. Needs the bridge:
                  NETCHECK <host> <port> <secret> "<prompt>"

TOOLCHK.EXE   prints what tool detection found, in the wire encoding:
                  TOOLCHK [<directory holding the INIs>]
              The same code the client runs, so what this prints is what
              the bridge is told.

GASTON.EXE    the GUI client. Reads GASTON.INI from its own directory.

TOOLSDET.INI  what to look for when reporting which tools this machine has.
              Installed as tools_detect.ini and REPLACED on every install.
              To add something of your own, put it in TOOLS.INI beside it:
              that file is never shipped and is read second, so it wins.

VIEW.CSS      how the transcript looks: colours, spacing, type. Installed
VIEW.TPL      as view.css and view.tpl. Edit either, restart the client,
              see the change -- no rebuild. Until stage 3 both lived inside
              the program and arrived over the wire, which meant you could
              not change how the client looks by changing the client.
              EXPORT.CSS is the handful of extra rules a saved transcript
              needs and the live view has no use for.

              All three are versioned against the client. A mismatch is a
              line in the status bar and in GASTON.LOG, never a refusal: a
              stale appearance beats no transcript. If view.tpl is missing
              or malformed the client says so and falls back to plain
              paragraphs, which is ugly and legible.

Copy these to C:\GASTON before running, so they can write their log files.
TXT
unix2dos -q "$STAGE/README.TXT" 2>/dev/null || sed -i 's/$/\r/' "$STAGE/README.TXT"

if command -v genisoimage >/dev/null; then MK=genisoimage
elif command -v mkisofs >/dev/null; then MK=mkisofs
elif command -v xorrisofs >/dev/null; then MK=xorrisofs
else echo "need genisoimage/mkisofs/xorrisofs" >&2; exit 1; fi

"$MK" -quiet -o "$OUT" -V GASTONTOOLS -iso-level 1 -J -r "$STAGE"
ls -lh "$OUT" | awk '{print "  built " $NF " (" $5 ")"}'
