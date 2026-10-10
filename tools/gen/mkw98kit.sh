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
# GREMOTE joins the kit (2026-10-10). The P200 has no CD and no network until
# somebody gives it one, and a hand-carried floppy is its only proven channel
# -- so the program that would let it be driven afterwards has to be on that
# floppy, or the first visit is a build and the second is the measurement.
# Its INI and its RunServices .REG come from the ISO's staging directory
# rather than being written again here: one heredoc for both kits, because
# two copies of a config file drift and the drift is silent.
GREMOTE="$HERE/build/win/gremote.exe"
GSTAGE="$HERE/vm/tools-stage"
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

if [ ! -f "$GREMOTE" ]; then
  echo "mkw98kit: $GREMOTE is missing. Run: make -C win gremote" >&2
  exit 1
fi
if [ ! -f "$GSTAGE/GREMOTE.INI" ] || [ ! -f "$GSTAGE/GREMOTE.REG" ]; then
  echo "mkw98kit: no $GSTAGE/GREMOTE.INI or .REG -- those are written by the
       ISO build, and this kit copies rather than repeats them.
       Run: bash tools/gen/mktools-iso.sh" >&2
  exit 1
fi

rm -rf "$STAGE"; mkdir -p "$STAGE"
cp "$PROBE"            "$STAGE/PROBE.EXE"
cp "$SRC/TEST.BAT"     "$STAGE/TEST.BAT"
cp "$SRC/README.TXT"   "$STAGE/README.TXT"
cp "$SRC/UITEST.HTM"   "$STAGE/UITEST.HTM"
cp "$DLL"              "$STAGE/HOOKDLL.DLL"
cp "$AUTO"             "$STAGE/AUTOCHK.EXE"
cp "$GREMOTE"          "$STAGE/GREMOTE.EXE"
cp "$GSTAGE/GREMOTE.INI" "$STAGE/GREMOTE.INI"
cp "$GSTAGE/GREMOTE.REG" "$STAGE/GREMOTE.REG"

# Every text file in the kit must be CRLF, and this is checked rather than
# trusted. COMMAND.COM will not run a .BAT with bare LF line endings, and
# the failure lands on whoever is sitting at the Windows machine, hours
# after the zip left here, with nothing to diagnose it from.
#
# It has already happened once: an edit read TEST.BAT in TEXT mode, which
# turns CRLF into LF on the way in, and wrote it back with a plain text
# write, which puts back the platform's ending -- \n on this side. Nothing
# looked wrong at any point.
for f in TEST.BAT README.TXT UITEST.HTM GREMOTE.INI GREMOTE.REG; do
  python3 - "$STAGE/$f" <<'CRLF' || exit 1
import sys
d = open(sys.argv[1], "rb").read()
crlf, lf = d.count(b"\r\n"), d.count(b"\n")
if crlf != lf:
    print(f"mkw98kit: {sys.argv[1]} has {lf - crlf} bare LF line endings;"
          " COMMAND.COM will not run it", file=sys.stderr)
    raise SystemExit(1)
CRLF
done
echo "  line endings: CRLF, all text files"

# IT HAS TO FIT ON A FLOPPY, and until now nothing said so. The kit is
# described everywhere as "a 1.44MB floppy" and this script had no size check
# at all, so the first file that pushed it over would have been found by
# somebody standing at a DOS prompt with a disk that would not take it.
FLOPPY=1474560
TOTAL=$(cat "$STAGE"/* | wc -c)
if [ "$TOTAL" -gt "$FLOPPY" ]; then
  echo "mkw98kit: the kit is $TOTAL bytes and a 1.44MB floppy holds $FLOPPY." >&2
  echo "          Drop something, or split it across two disks on purpose." >&2
  exit 1
fi
echo "  size: $TOTAL bytes of $FLOPPY ($((TOTAL * 100 / FLOPPY))% of a floppy)"

rm -f "$OUT"
( cd "$STAGE" && zip -q -X "$OUT" PROBE.EXE AUTOCHK.EXE HOOKDLL.DLL \
      GREMOTE.EXE GREMOTE.INI GREMOTE.REG TEST.BAT README.TXT UITEST.HTM )

echo "built $OUT"
unzip -l "$OUT" | sed -n '3,$p'
echo
echo "--- import audit of the exe that is actually in the zip ---"
python3 "$HERE/tools/check/imports.py" "$STAGE/PROBE.EXE" || true
echo "--- and of the DLL, which gets mapped into other people's processes ---"
python3 "$HERE/tools/check/imports.py" "$STAGE/HOOKDLL.DLL" || true
