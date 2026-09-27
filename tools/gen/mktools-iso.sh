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
# own, which is why SETUP.BAT below copies the long name.
want "$HERE/build/win/gremote.exe" "GREMOTE.EXE"
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
# Shipping a hand-maintained copy of the CSS would be the obvious way and the
# wrong one: two copies of a stylesheet drift, which is the bug rc_html::user
# carries a long comment about -- the guest stripped whitespace differently
# from the bridge and one message rendered two ways.
#
# So it is emitted from bridge/view/html.py at package time. The bridge's DOC
# frame still wins at runtime: set_document compares and re-installs when the
# two differ, which is also what makes a CSS change reach the guest on a
# reconnect rather than a reinstall.
# The transcript's spinner. A real file because IE5 animates GIFs but has no
# data: URI support, so it cannot be inlined into the document.
need "$HERE/win/resources/spin.gif" "SPIN.GIF"

if ! python3 -c "
import sys; sys.path.insert(0, '$HERE')
from bridge.view.html import document
open('$STAGE/VIEW.HTM', 'w', encoding='cp1252').write(document())
" 2>/dev/null; then
    echo "  WARNING: could not generate VIEW.HTM from bridge/view/html.py;" >&2
    echo "           the guest will start with no document until DOC arrives" >&2
fi

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
REM The cold-start transcript document. Without it the app shows a bare grey
REM rectangle until a bridge answers, which is also when it most needs to say
REM something. Long name deliberately: the app opens view.htm beside itself.
REM Long name deliberately, for the reason given above: the
REM detector opens tools_detect.ini beside the exe, and ISO level 1 can only
REM carry TOOLSDET.INI. TOOLS.INI is the user's and is NEVER shipped -- it is
REM read second so their additions win, and copying one over it on every
REM install would throw those away.
REM And gremote.ini. The client's own ini needs no such line any more -- it
REM is gaston.ini, which is already 8.3 -- but gremote's is not: the server
REM joins the exe's directory with the literal long name, and on FAT
REM GREMOTE.INI is a DIFFERENT FILE. Without this line every key reads back
REM empty and the first thing anyone sees is a complaint about the port,
REM which is the one setting that was not wrong.
REM /Y and a NAMED DESTINATION on every one of these, for the reason the
REM gaston.ini line above gives and for one more found on the guest: after
REM the CD is swapped, a COPY to a DIRECTORY silently kept the old file
REM while the same copy to a named file replaced it. Win98 serves fresh
REM directory entries with stale data blocks for a while after a media
REM change, so these are the copies most likely to land the previous disc.
IF EXIST D:\GREMOTE.INI COPY /Y D:\GREMOTE.INI C:\GASTON\gremote.ini > NUL
IF EXIST D:\GREMOTE.REG COPY /Y D:\GREMOTE.REG C:\GASTON\GREMOTE.REG > NUL
IF EXIST D:\TOOLSDET.INI COPY /Y D:\TOOLSDET.INI C:\GASTON\tools_detect.ini > NUL
IF EXIST D:\VIEW.HTM COPY /Y D:\VIEW.HTM C:\GASTON\view.htm > NUL
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

Copy these to C:\GASTON before running, so they can write their log files.
TXT
unix2dos -q "$STAGE/README.TXT" 2>/dev/null || sed -i 's/$/\r/' "$STAGE/README.TXT"

if command -v genisoimage >/dev/null; then MK=genisoimage
elif command -v mkisofs >/dev/null; then MK=mkisofs
elif command -v xorrisofs >/dev/null; then MK=xorrisofs
else echo "need genisoimage/mkisofs/xorrisofs" >&2; exit 1; fi

"$MK" -quiet -o "$OUT" -V GASTONTOOLS -iso-level 1 -J -r "$STAGE"
ls -lh "$OUT" | awk '{print "  built " $NF " (" $5 ")"}'
