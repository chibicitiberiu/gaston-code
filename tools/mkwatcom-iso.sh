#!/usr/bin/env bash
# Build vm/watcom.iso: an Open Watcom the GUEST can compile with.
#
# NOT the toolchain this repo builds with. That one is Open Watcom 2.0 hosted
# on Linux -- `/usr/bin/watcom/binnt` there holds debug servers and DLLs and
# no compilers at all, because a Linux build has no reason to ship Windows
# executables. Compiling ON the Win98 box needs a Windows-hosted tree, and
# 1.9 is the last release that names Windows 95/98/ME as a supported host.
#
# The installer is a ZIP with a PE stub in front of it, so there is no GUI
# setup to drive on the guest: unzip it here, ship the parts that matter, and
# copy them across. Every filename in those parts is already 8.3 and every
# tool imports nothing but USER32 and KERNEL32 -- both checked before this
# script was written, and both worth re-checking if the version moves.
#
#   tools/mkwatcom-iso.sh [path-to-open-watcom-c-win32-1.9.exe]
#
# With no argument it downloads the installer beside the ISO and keeps it, so
# a rebuild costs nothing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

URL=https://github.com/open-watcom/open-watcom-1.9/releases/download/ow1.9/open-watcom-c-win32-1.9.exe
#: Checked once, against the file this was developed with. A toolchain that
#: silently changed underneath is the kind of thing that costs a day.
SHA=040c910aba304fdb5f39b8fe508cd3c772b1da1f91a58179fa0895e0b2bf190b

VM="$PWD/vm"
SRC="${1:-$VM/open-watcom-c-win32-1.9.exe}"
STAGE="$VM/watcom-stage"
OUT="$VM/watcom.iso"

mkdir -p "$VM"
if [ ! -f "$SRC" ]; then
    echo "downloading $(basename "$SRC")..."
    curl -fsSL -o "$SRC.part" "$URL"
    mv "$SRC.part" "$SRC"
fi
echo "$SHA  $SRC" | sha256sum -c - >/dev/null \
    || { echo "checksum mismatch on $SRC -- refusing" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE/WATCOM"

# WHAT GOES, AND WHAT DOES NOT.
#
#   binnt   the Win32-hosted tools. These are what run on the guest.
#   h       headers, including h/nt for the Win32 API.
#   lib386  32-bit libraries, including lib386/nt and lib386/win.
#   eddat   the message files. wlink and wcc386 read these to turn an error
#           number into a sentence, and without them every diagnostic is a
#           bare code -- which looks like a broken install and is not.
#   samples something to compile that is known to be correct, for the first
#           five minutes on a new toolchain.
#
# binw (51M, DOS-hosted), binp (OS/2), binl (Linux), lib286 and docs are all
# left out. None is needed to build a Win32 program and together they are
# more than everything above.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "unpacking..."
7z x -bso0 -bsp0 -o"$TMP" "$SRC"
for part in binnt h lib386 eddat samples; do
    [ -d "$TMP/$part" ] || { echo "missing $part in the installer" >&2; exit 1; }
    cp -a "$TMP/$part" "$STAGE/WATCOM/"
done

# Set up on the guest by running this, not by hand. Win98's default
# environment is 256 bytes and these variables do not fit in it -- see the
# SHELL= line it prints, which is the part everybody forgets and which shows
# up later as "Out of environment space" and a PATH that is silently short.
cat > "$STAGE/WATCOM/SETVARS.BAT" <<'BAT'
@ECHO OFF
REM Open Watcom 1.9, for building on this machine.
REM Run from AUTOEXEC.BAT, or by hand in a DOS box for one session.
SET WATCOM=C:\WATCOM
SET PATH=%PATH%;C:\WATCOM\BINNT
SET INCLUDE=C:\WATCOM\H;C:\WATCOM\H\NT
SET EDPATH=C:\WATCOM\EDDAT
ECHO Open Watcom 1.9 ready.  wcc386 / wpp386 / wlink / wmake / wrc
BAT

cat > "$STAGE/README.TXT" <<'TXT'
Open Watcom 1.9, Windows-hosted, for compiling ON this machine.

  XCOPY D:\WATCOM C:\WATCOM /S /E /I
  CALL C:\WATCOM\SETVARS.BAT

To make it permanent, add this line to C:\AUTOEXEC.BAT:

  CALL C:\WATCOM\SETVARS.BAT

If the machine then says "Out of environment space", C:\CONFIG.SYS needs a
bigger one.  Add or edit:

  SHELL=C:\COMMAND.COM /E:4096 /P

and reboot.  The default is 256 bytes and PATH plus INCLUDE do not fit.

A first program:

  CD C:\WATCOM\SAMPLES
  wcl386 -l=nt hello.c

Tools are in C:\WATCOM\BINNT.  wcl386 drives the compiler and linker in one
step; wcc386 (C), wpp386 (C++), wlink, wmake and wrc are the separate pieces.
TXT
sed -i 's/$/\r/' "$STAGE/README.TXT"

if command -v genisoimage >/dev/null; then MK=genisoimage
elif command -v mkisofs >/dev/null; then MK=mkisofs
elif command -v xorrisofs >/dev/null; then MK=xorrisofs
else echo "need genisoimage/mkisofs/xorrisofs" >&2; exit 1; fi

# -iso-level 1 and -J, the same as the tools ISO: 8.3 names that MS-DOS mode
# can read, with Joliet alongside for Windows. Everything shipped above is
# already 8.3, so the two views agree.
"$MK" -quiet -o "$OUT" -V WATCOM19 -iso-level 1 -J -r "$STAGE"
du -h "$OUT" | awk '{print "  built " $2 " (" $1 ")"}'
