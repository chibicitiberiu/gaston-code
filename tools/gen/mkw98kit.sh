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
# GUPDATE joins the kit (2026-10-15, stage 5). Without it the box can be
# reached but never UPDATED: win/remote/control.cpp refuses UPDATE unless the
# updater sits beside the running image, and there is no remote QUIT to fall
# back on (docs/REMOTE.md, "There is no remote QUIT"). So the first build that
# lands is the last one until somebody walks over -- and since UPDATE is also
# the only way to leave the port in TIME_WAIT on purpose, the P200 checklist's
# bind bullet has no instrument without it either.
GUPDATE="$HERE/build/win/gupdate.exe"
# FOCUSPROBE answers REMOTE.md's SPI_SETFOREGROUNDLOCKTIMEOUT question, which
# BACKLOG 9 keeps open until a real machine agrees. It prints to the console by
# its own design, so it is run through a redirect and the file fetched back.
FOCUSPROBE="$HERE/build/win/focusprobe.exe"
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
if [ ! -f "$GUPDATE" ]; then
  echo "mkw98kit: $GUPDATE is missing. Run: make -C win gupdate" >&2
  exit 1
fi
if [ ! -f "$FOCUSPROBE" ]; then
  echo "mkw98kit: $FOCUSPROBE is missing. Run: make -C win focusprobe" >&2
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
cp "$GUPDATE"          "$STAGE/GUPDATE.EXE"
cp "$FOCUSPROBE"       "$STAGE/FOCUSPRB.EXE"
cp "$GSTAGE/GREMOTE.INI" "$STAGE/GREMOTE.INI"
cp "$GSTAGE/GREMOTE.REG" "$STAGE/GREMOTE.REG"
# The deflate corpus is GENERATED here rather than copied from the ISO
# staging, unlike the INI and the .REG: it is not a config, it is data derived
# from this tree, and a staged copy could go stale against the sources it is
# built from without anything saying so.
python3 "$HERE/tools/gen/mkcorpus.py" -o "$STAGE/CORPUS.TXT" | sed 's/^/  /'

# Every text file in the kit must be CRLF, and this is checked rather than
# trusted. COMMAND.COM will not run a .BAT with bare LF line endings, and
# the failure lands on whoever is sitting at the Windows machine, hours
# after the zip left here, with nothing to diagnose it from.
#
# It has already happened once: an edit read TEST.BAT in TEXT mode, which
# turns CRLF into LF on the way in, and wrote it back with a plain text
# write, which puts back the platform's ending -- \n on this side. Nothing
# looked wrong at any point.
for f in TEST.BAT README.TXT UITEST.HTM GREMOTE.INI GREMOTE.REG CORPUS.TXT; do
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

# WHICH GREMOTE.INI IS THIS, AND IS IT SAFE TO CARRY. The ISO build writes a
# permissive default -- bind=0.0.0.0, allow= empty, automation=2, and a secret
# that is NOT the one gremote refuses to start on -- because under QEMU
# user-mode networking nothing else can reach the port anyway. On a LAN box
# that same file means anything that can reach 7401 can walk the desktop, type
# into it and reboot it.
#
# tools/gen/mktools-iso.sh:129 lets a local vm/gremote.ini override the
# heredoc, and it does so with `2>/dev/null || true` -- so a missing override
# is silent, and the permissive default travels without anybody deciding to
# send it. This is the kit that goes to a REAL machine, so it says which file
# it has and refuses the default unless somebody opts in.
INI="$STAGE/GREMOTE.INI"
# tr -d '\r' is load-bearing, and it was missing on the first run of this
# block: the staged INI is CRLF (the check just above requires it), so
# `secret=vm-secret` reads back as `vm-secret\r`, which compares equal to
# nothing, and `allow=` reads back as `\r`, which is not empty. Both tests
# silently passed a permissive file.
KIT_SECRET=$(sed -n 's/^secret=//p' "$INI" | head -1 | tr -d '\r')
KIT_ALLOW=$(sed -n 's/^allow=//p' "$INI" | head -1 | tr -d '\r')
echo "  gremote.ini: secret=${KIT_SECRET:-<unset>} allow=${KIT_ALLOW:-<any host>}"
if [ "$KIT_SECRET" = "vm-secret" ] || [ -z "$KIT_ALLOW" ]; then
  if [ "${GASTON_KIT_PERMISSIVE_INI:-0}" != "1" ]; then
    echo "mkw98kit: this GREMOTE.INI is the VM default -- secret=vm-secret
       and/or an empty allow=, which grants the desktop to anything that can
       reach port 7401. The kit goes to a real machine on a real LAN.

       Write vm/gremote.ini with a secret of your own and an allow= that
       names the host that will drive the box, then re-run tools/gen/
       mktools-iso.sh so this kit picks it up. vm/gremote.ini is gitignored
       so the secret stays out of the history.

       To build the kit anyway (a VM, or an air-gapped bench):
       GASTON_KIT_PERMISSIVE_INI=1" >&2
    exit 1
  fi
  echo "  gremote.ini: permissive default, carried on purpose"
fi

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
      GREMOTE.EXE GUPDATE.EXE FOCUSPRB.EXE GREMOTE.INI GREMOTE.REG \
      CORPUS.TXT TEST.BAT README.TXT UITEST.HTM )

echo "built $OUT"
unzip -l "$OUT" | sed -n '3,$p'
echo
# The audit covers EVERY binary in the zip, not just the probe. A single
# NT-only static import kills an image at load, before WinMain, with no log
# written -- and the kit now carries four exes, two of which (GREMOTE, GUPDATE)
# are the box's only way back if something goes wrong. An unaudited image on
# the floppy is a walk across town.
echo "--- import audit of every binary that is actually in the zip ---"
for b in PROBE.EXE AUTOCHK.EXE GREMOTE.EXE GUPDATE.EXE FOCUSPRB.EXE; do
  printf '  %-13s ' "$b"
  python3 "$HERE/tools/check/imports.py" "$STAGE/$b" || true
done
echo "--- and the DLL, which gets mapped into other people's processes ---"
printf '  %-13s ' HOOKDLL.DLL
python3 "$HERE/tools/check/imports.py" "$STAGE/HOOKDLL.DLL" || true
