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

# Where the kit is written for carrying, if anywhere: a mounted USB volume,
# e.g. --dest /run/media/tibi/5A21-DE92. The zip is still built either way --
# it is the transport for a copy that travels as a file rather than on a disk.
DEST="${GASTON_KIT_DEST:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="${2:-}"; shift 2 ;;
    --dest=*) DEST="${1#--dest=}"; shift ;;
    *) echo "mkw98kit: unknown argument $1 (only --dest DIR)" >&2; exit 1 ;;
  esac
done

PROBE="$HERE/build/win/probe.exe"
DLL="$HERE/build/win/HOOKDLL.DLL"
AUTO="$HERE/build/win/autocheck.exe"
# GREMOTE joins the kit (2026-10-10). The P200 has no CD, so the kit is
# hand-carried -- so the program that would let it be driven afterwards has to
# be on the same trip, or the first visit is a build and the second is the
# measurement.
#
# THE CHANNEL IS A USB STICK AS WELL AS A FLOPPY (2026-10-23). The box reads
# one, which retires the 1.44MB ceiling as a hard limit: see --dest below. The
# 8.3 names are still the rule and now earn their keep twice over -- a FAT32
# volume written from Linux stores a pure uppercase 8.3 name as a short entry
# with no long-name record, so what DIR shows on the box is what was written.
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
# Built beside probe.exe by `make -C win probe`; see the copy below.
CORPUS="$HERE/build/win/CORPUS.TXT"
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
if [ "$HERE/research/guest/probe.c" -nt "$PROBE" ]; then
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
if [ ! -f "$CORPUS" ]; then
  echo "mkw98kit: $CORPUS is missing. Run: make -C win probe" >&2
  exit 1
fi
# A corpus older than the sources it is built from is a corpus whose crc32 no
# longer matches what the tree would produce, and the whole point of that crc
# is that two runs can be checked for being the same experiment.
#
# CHECKED BY CRC, NOT BY MTIME. An mtime test against mkcorpus.py catches an
# edit to the generator and misses an edit to any of the eleven files the
# corpus is built FROM -- and three of those are documents in docs/, which get
# edited constantly. So the corpus is regenerated in memory and compared: if
# the carried bytes are not the bytes this tree produces, the crc printed
# beside a P200 figure would name a corpus that no longer exists, which is the
# exact confusion the crc was added to end.
KIT_CRC=$(python3 -c '
import sys, zlib
sys.stdout.write("%08x" % (zlib.crc32(open(sys.argv[1], "rb").read()) & 0xFFFFFFFF))
' "$CORPUS")
TREE_CRC=$(python3 "$HERE/research/deflate-speeds/mkcorpus.py" --check \
           | sed -n 's/^mkcorpus: crc32 //p')
if [ "$KIT_CRC" != "$TREE_CRC" ]; then
  echo "mkw98kit: $CORPUS has crc32 $KIT_CRC and this tree now produces
       $TREE_CRC -- one of the eleven files it is built from has been edited
       since. Run: make -C win probe" >&2
  exit 1
fi
echo "  corpus: $(wc -c < "$CORPUS") bytes, crc32 $KIT_CRC, matches the tree"
# WHERE THE INI COMES FROM, IN ORDER. vm/gremote.ini first if it exists -- it
# is the per-box file, gitignored, and the only one that carries a real secret
# -- then the ISO build's staged copy, which is the permissive default.
#
# It USED to be the staged copy only, which meant carrying a per-box secret
# required building a whole tools ISO first (mktools-iso.sh is where the
# override is read) for the sake of one 980-byte text file. That was fine while
# the kit was a floppy nobody could put a secret on. It is not fine now that
# the kit IS the channel, so the override is read here too, from the same path
# mktools-iso.sh reads it from -- one file, two readers, no third copy.
GREMOTE_INI_SRC="$GSTAGE/GREMOTE.INI"
GREMOTE_INI_WHENCE="the ISO build's staged default"
if [ -f "$HERE/vm/gremote.ini" ]; then
  GREMOTE_INI_SRC="$HERE/vm/gremote.ini"
  GREMOTE_INI_WHENCE="vm/gremote.ini (per-box, gitignored)"
fi
if [ ! -f "$GREMOTE_INI_SRC" ] || [ ! -f "$GSTAGE/GREMOTE.REG" ]; then
  echo "mkw98kit: no gremote INI or .REG to copy. The .REG and the default INI
       are written by the ISO build, and this kit copies rather than repeats
       them; a per-box INI goes in vm/gremote.ini.
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
cp "$GREMOTE_INI_SRC"    "$STAGE/GREMOTE.INI"
cp "$GSTAGE/GREMOTE.REG" "$STAGE/GREMOTE.REG"
echo "  gremote.ini: from $GREMOTE_INI_WHENCE"
# Normalised rather than trusted, and the strip comes first so a file that is
# already CRLF does not come out CRCRLF. The CRLF check below would catch a
# bare-LF override, but catching it is worse than fixing it: the person who
# wrote vm/gremote.ini in a Linux editor did nothing wrong.
python3 - "$STAGE/GREMOTE.INI" <<'EOL'
import sys
p = sys.argv[1]
# Read fully, THEN open for writing. The one-liner form evaluates
# open(p, "wb") before its argument, truncating the file it is about to
# read, and the staged INI came out zero bytes -- which the safety gate
# below then read as "secret unset, allow empty" and refused, correctly,
# for the wrong reason.
d = open(p, "rb").read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
open(p, "wb").write(d)
EOL
# The deflate corpus is COPIED from beside probe.exe, where `make -C win
# probe` builds it, rather than generated a second time here. `probe deflate`
# reads it from its own directory and refuses without it, so the build already
# has to produce it -- and two generators for one file is two files that drift.
# The copy is what goes on the floppy; the probe on the floppy reads it from
# its own directory there exactly as it does in build/win.
cp "$CORPUS"          "$STAGE/CORPUS.TXT"

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
#
# WITH --dest THE CEILING IS THE DESTINATION'S (2026-10-23). The P200 reads a
# USB stick, which has four orders of magnitude more room than a floppy, and a
# kit that is 60% of a floppy is 0.003% of the stick. So the floppy figure is
# still printed -- it is the fallback channel and worth knowing -- but it only
# REFUSES when no destination was named, because then a floppy is what this is
# for. The destination's own free space is checked instead.
FLOPPY=1474560
TOTAL=$(cat "$STAGE"/* | wc -c)
if [ "$TOTAL" -gt "$FLOPPY" ]; then
  if [ -z "$DEST" ]; then
    echo "mkw98kit: the kit is $TOTAL bytes and a 1.44MB floppy holds $FLOPPY." >&2
    echo "          Drop something, split it across two disks on purpose, or" >&2
    echo "          pass --dest DIR to write it to a volume that can hold it." >&2
    exit 1
  fi
  echo "  size: $TOTAL bytes -- OVER a 1.44MB floppy, so the floppy is no"
  echo "        longer a fallback for this kit. Going to $DEST instead."
else
  echo "  size: $TOTAL bytes of $FLOPPY ($((TOTAL * 100 / FLOPPY))% of a floppy)"
fi

# Named once, because the zip and --dest both need the list and two lists is
# one list that will be missing a file.
KIT_FILES=(PROBE.EXE AUTOCHK.EXE HOOKDLL.DLL GREMOTE.EXE GUPDATE.EXE
           FOCUSPRB.EXE GREMOTE.INI GREMOTE.REG CORPUS.TXT TEST.BAT
           README.TXT UITEST.HTM)

rm -f "$OUT"
( cd "$STAGE" && zip -q -X "$OUT" "${KIT_FILES[@]}" )

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

# WRITING IT TO THE VOLUME THAT WILL BE CARRIED. The box reads a USB stick, so
# the kit no longer has to become a disk image first.
#
# THE COPY IS VERIFIED BYTE FOR BYTE, not by size. This project has been bitten
# twice by media that reported the right length and served the wrong blocks (a
# live CD swap the guest kept reading the old disc through), and a stick that
# was pulled before the page cache flushed fails exactly the same way -- with
# a DIR that looks perfect. So: sync, then cmp every file, then say so.
if [ -n "$DEST" ]; then
  echo
  echo "--- writing the kit to $DEST ---"
  if [ ! -d "$DEST" ]; then
    echo "mkw98kit: $DEST is not a directory. Mount the volume first." >&2
    exit 1
  fi
  if [ ! -w "$DEST" ]; then
    echo "mkw98kit: $DEST is not writable by $(id -un)." >&2
    exit 1
  fi
  FREE=$(df -P --block-size=1 "$DEST" | awk 'NR==2 {print $4}')
  if [ "$FREE" -lt "$TOTAL" ]; then
    echo "mkw98kit: $DEST has $FREE bytes free and the kit is $TOTAL." >&2
    exit 1
  fi
  for f in "${KIT_FILES[@]}"; do
    cp "$STAGE/$f" "$DEST/$f"
  done
  sync -f "$DEST" 2>/dev/null || sync
  BAD=0
  for f in "${KIT_FILES[@]}"; do
    if cmp -s "$STAGE/$f" "$DEST/$f"; then
      printf '  %-13s %8d  ok\n' "$f" "$(wc -c < "$DEST/$f")"
    else
      printf '  %-13s %8s  DIFFERS FROM THE STAGED COPY\n' "$f" "$(wc -c < "$DEST/$f" 2>/dev/null || echo '-')"
      BAD=$((BAD + 1))
    fi
  done
  if [ "$BAD" -ne 0 ]; then
    echo "mkw98kit: $BAD file(s) on $DEST do not match what was staged." >&2
    echo "          Do NOT carry this volume. Re-mount it and run again." >&2
    exit 1
  fi
  echo "  verified: ${#KIT_FILES[@]} files, byte for byte, after a flush"
  echo "  $DEST now has $(df -P --block-size=1M "$DEST" | awk 'NR==2 {print $4}') MB free"
  echo
  echo "  Safe to unmount. On the box the kit is on whatever letter the stick"
  echo "  takes -- README.TXT calls that letter X: and says to check it first."
fi
