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
cp "$HERE/win/retroclaude.exe" "$STAGE/RETROCL.EXE" 2>/dev/null || true
cp "$HERE/win/netcheck.exe"    "$STAGE/NETCHECK.EXE" 2>/dev/null || true
cp "$HERE/spike/hello98.exe"   "$STAGE/HELLO98.EXE" 2>/dev/null || true
cp "$HERE/win/retroclaude.ini" "$STAGE/RETROCL.INI" 2>/dev/null || true

cat > "$STAGE/README.TXT" <<'TXT'
RetroClaude test payload
========================

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
