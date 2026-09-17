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
cp "$HERE/win/probe.exe"       "$STAGE/PROBE.EXE"  2>/dev/null || true
cp "$HERE/win/htmlspike.exe"   "$STAGE/HTMLSPK.EXE" 2>/dev/null || true
cp "$HERE/win/retroclaude.exe" "$STAGE/RETROCL.EXE" 2>/dev/null || true
cp "$HERE/win/netcheck.exe"    "$STAGE/NETCHECK.EXE" 2>/dev/null || true
cp "$HERE/spike/hello98.exe"   "$STAGE/HELLO98.EXE" 2>/dev/null || true
# vm/retrocl.ini points at 10.0.2.2 (the QEMU user-mode host) and wins over
# the Wine-tier default of 127.0.0.1 when it exists.
cp "$HERE/win/retroclaude.ini" "$STAGE/RETROCL.INI" 2>/dev/null || true
cp "$HERE/vm/retrocl.ini"      "$STAGE/RETROCL.INI" 2>/dev/null || true

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

RETROCL.EXE   the GUI client. Reads RETROCL.INI from its own directory.

Copy these to C:\RETRO before running, so they can write their log files.
TXT
unix2dos -q "$STAGE/README.TXT" 2>/dev/null || sed -i 's/$/\r/' "$STAGE/README.TXT"

if command -v genisoimage >/dev/null; then MK=genisoimage
elif command -v mkisofs >/dev/null; then MK=mkisofs
elif command -v xorrisofs >/dev/null; then MK=xorrisofs
else echo "need genisoimage/mkisofs/xorrisofs" >&2; exit 1; fi

"$MK" -quiet -o "$OUT" -V RETROTOOLS -iso-level 1 -J -r "$STAGE"
ls -lh "$OUT" | awk '{print "  built " $NF " (" $5 ")"}'
