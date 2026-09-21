#!/usr/bin/env bash
# Build a CD image carrying the RetroClaude binaries and the Phase 0 test
# programs into the guest.
#
# A CD rather than a floppy because the binaries already exceed 1.44MB, and
# rather than the network because networking is itself one of the things
# being tested -- this has to work before the guest can talk to the bridge.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$HERE/vm/tools-stage"
OUT="$HERE/vm/tools.iso"

rm -rf "$STAGE"; mkdir -p "$STAGE"

# 8.3 names throughout: the guest may see this via a driver that does not
# surface long filenames, and a truncated name is a confusing failure.
cp "$HERE/build/win/probe.exe"       "$STAGE/PROBE.EXE"  2>/dev/null || true
cp "$HERE/win/htmlspike.exe"   "$STAGE/HTMLSPK.EXE" 2>/dev/null || true
cp "$HERE/build/win/retroclaude.exe" "$STAGE/RETROCL.EXE" 2>/dev/null || true
cp "$HERE/build/win/netcheck.exe"    "$STAGE/NETCHECK.EXE" 2>/dev/null || true
# The collision-mode check. It runs green under Wine and that proves the
# code, never the platform -- CopyFile, MoveFile and the 8.3 aliasing
# underneath them are exactly the layer Wine reimplements.
cp "$HERE/build/win/workcheck.exe"   "$STAGE/WORKCHK.EXE" 2>/dev/null || true
# Tool detection, printing exactly the bytes HELLO carries. On the guest it
# answers two things Wine cannot: how long a real FAT scan takes, and whether
# a path on a drive with no disk in it raises the system-modal "There is no
# disk in drive A:" that would freeze the app's message pump.
cp "$HERE/build/win/toolcheck.exe"   "$STAGE/TOOLCHK.EXE" 2>/dev/null || true
# vm/retrocl.ini points at 10.0.2.2 (the QEMU user-mode host) and wins over
# the Wine-tier default of 127.0.0.1 when it exists.
cp "$HERE/win/retroclaude.ini" "$STAGE/RETROCL.INI" 2>/dev/null || true
cp "$HERE/vm/retrocl.ini"      "$STAGE/RETROCL.INI" 2>/dev/null || true
# What to look for on the machine. Without this on the CD the detector finds
# nothing and the feature is inert -- it fails silently, because "no INI" and
# "nothing installed" produce the same empty report by design.
cp "$HERE/win/tools_detect.ini" "$STAGE/TOOLSDET.INI" 2>/dev/null || true

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
cp "$HERE/win/app/spin.gif" "$STAGE/SPIN.GIF" 2>/dev/null || true

if ! python3 -c "
import sys; sys.path.insert(0, '$HERE')
from bridge.view.html import document
open('$STAGE/VIEW.HTM', 'w', encoding='cp1252').write(document())
" 2>/dev/null; then
    echo "  WARNING: could not generate VIEW.HTM from bridge/view/html.py;" >&2
    echo "           the guest will start with no document until DOC arrives" >&2
fi

# One command to type blind through QMP send-key, rather than six.
cat > "$STAGE/SETUP.BAT" <<'BAT'
@ECHO OFF
REM COMMAND.COM rejects a trailing backslash on a COPY destination
REM ("Invalid directory"), so C:\RETRO is written without one below.
IF NOT EXIST C:\RETRO\NUL MD C:\RETRO
COPY D:\*.EXE C:\RETRO > NUL
REM The app reads retroclaude.ini, NOT retrocl.ini. ISO level 1 forces the 8.3
REM name on the CD, so a plain "COPY D:\*.INI" lands a file the client never
REM opens -- it then falls back to its compiled-in defaults and, in the case
REM that cost an hour, ran with an empty sync root and no error anywhere.
REM Name the destination explicitly. The Win98 DOS box speaks long filenames.
COPY D:\RETROCL.INI C:\RETRO\retroclaude.ini > NUL
REM The cold-start transcript document. Without it the app shows a bare grey
REM rectangle until a bridge answers, which is also when it most needs to say
REM something. Long name deliberately: the app opens view.htm beside itself.
REM Long name again, and for the same reason as retroclaude.ini above: the
REM detector opens tools_detect.ini beside the exe, and ISO level 1 can only
REM carry TOOLSDET.INI. TOOLS.INI is the user's and is NEVER shipped -- it is
REM read second so their additions win, and copying one over it on every
REM install would throw those away.
IF EXIST D:\TOOLSDET.INI COPY D:\TOOLSDET.INI C:\RETRO\tools_detect.ini > NUL
IF EXIST D:\VIEW.HTM COPY D:\VIEW.HTM C:\RETRO\view.htm > NUL
IF EXIST D:\SPIN.GIF COPY D:\SPIN.GIF C:\RETRO\spin.gif > NUL
C:
CD \RETRO
ECHO Copied to C:\RETRO.  Run PROBE to start the Phase 0 experiments.
BAT
sed -i 's/$/\r/' "$STAGE/SETUP.BAT"

cat > "$STAGE/README.TXT" <<'TXT'
RetroClaude test payload
========================

SETUP.BAT     copies everything to C:\RETRO and cd's there.

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

RETROCL.EXE   the GUI client. Reads RETROCL.INI from its own directory.

TOOLSDET.INI  what to look for when reporting which tools this machine has.
              Installed as tools_detect.ini and REPLACED on every install.
              To add something of your own, put it in TOOLS.INI beside it:
              that file is never shipped and is read second, so it wins.

Copy these to C:\RETRO before running, so they can write their log files.
TXT
unix2dos -q "$STAGE/README.TXT" 2>/dev/null || sed -i 's/$/\r/' "$STAGE/README.TXT"

if command -v genisoimage >/dev/null; then MK=genisoimage
elif command -v mkisofs >/dev/null; then MK=mkisofs
elif command -v xorrisofs >/dev/null; then MK=xorrisofs
else echo "need genisoimage/mkisofs/xorrisofs" >&2; exit 1; fi

"$MK" -quiet -o "$OUT" -V RETROTOOLS -iso-level 1 -J -r "$STAGE"
ls -lh "$OUT" | awk '{print "  built " $NF " (" $5 ")"}'
