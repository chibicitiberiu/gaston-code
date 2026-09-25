#!/usr/bin/env bash
# Build the zip that goes to the real Windows 98 machine.
#
# The emulated guest answers "does this work" and, running on a modern host
# under TCG, answers nothing at all about speed. Every millisecond measured
# there is a number about a Ryzen. This kit is how the questions get asked
# on hardware that can answer them.
#
# 8.3 names throughout: the machine may be reached through something that
# does not surface long filenames, and a truncated name is a confusing
# failure.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
HERE="$REPO"
SRC="$HERE/tools/gen/w98kit"
STAGE="$HERE/build/w98kit"
OUT="$HERE/build/gaston-w98-tests.zip"

PROBE="$HERE/build/win/probe.exe"
DLL="$HERE/build/win/HOOKDLL.DLL"
AUTO="$HERE/build/win/autocheck.exe"
if [ ! -f "$PROBE" ]; then
  echo "mkw98kit: $PROBE is missing. Run: make -C win probe" >&2
  exit 1
fi
# The injection test is inert without it, and inert LOOKS like "injection
# does not work on this machine" -- the exact wrong conclusion.
if [ ! -f "$DLL" ]; then
  echo "mkw98kit: $DLL is missing. Run: make -C win hookdll" >&2
  exit 1
fi
if [ ! -f "$AUTO" ]; then
  echo "mkw98kit: $AUTO is missing. Run: make -C win autocheck" >&2
  exit 1
fi

# Refuse to ship a probe older than its source. A kit built from a stale
# exe looks identical to a fresh one and the results come back answering
# last week's question.
if [ "$HERE/win/checks/probe.c" -nt "$PROBE" ]; then
  echo "mkw98kit: probe.c is newer than probe.exe. Run: make -C win probe" >&2
  exit 1
fi

rm -rf "$STAGE"; mkdir -p "$STAGE"
cp "$PROBE"            "$STAGE/PROBE.EXE"
cp "$SRC/TEST.BAT"     "$STAGE/TEST.BAT"
cp "$SRC/README.TXT"   "$STAGE/README.TXT"
cp "$SRC/UITEST.HTM"   "$STAGE/UITEST.HTM"
cp "$DLL"              "$STAGE/HOOKDLL.DLL"
cp "$AUTO"             "$STAGE/AUTOCHK.EXE"

rm -f "$OUT"
( cd "$STAGE" && zip -q -X "$OUT" PROBE.EXE AUTOCHK.EXE HOOKDLL.DLL TEST.BAT README.TXT UITEST.HTM )

echo "built $OUT"
unzip -l "$OUT" | sed -n '3,$p'
echo
echo "--- import audit of the exe that is actually in the zip ---"
python3 "$HERE/tools/check/imports.py" "$STAGE/PROBE.EXE" || true
echo "--- and of the DLL, which gets mapped into other people's processes ---"
python3 "$HERE/tools/check/imports.py" "$STAGE/HOOKDLL.DLL" || true
