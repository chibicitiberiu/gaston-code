#!/usr/bin/env bash
# mkrelease.sh -- the two GUEST release packages, built the same way here and
# in CI.
#
# WHY A SCRIPT AND NOT WORKFLOW YAML. Everything below runs on a developer's
# machine with no GitHub involved, which means the packaging can be tested
# without pushing a tag and watching a runner. The workflow
# (.github/workflows/release.yml) installs a toolchain, calls this, and
# uploads what it produced; it holds no packaging logic of its own. When the
# release is wrong, it is wrong here, where it can be run again in four
# seconds.
#
# WHAT IT PRODUCES, and the split is the two programs rather than two halves
# of one:
#
#   gaston-<ver>-win98.zip    the GUI client a person sits in front of
#   gremote-<ver>-win98.zip   the server that lets a machine in another room
#                             be driven, which is a separate decision with
#                             separate consequences -- it opens a port
#
# The host-side Python is NOT in either. It is one wheel of its own
# (tools/gen/mkwheel.sh) because it runs on a modern machine, and bundling
# it with a 1998 binary would mean downloading CPython to copy a 262 KB exe
# onto a floppy.
#
# NOTHING HERE WRITES A SECRET, and the two programs answer that
# differently -- which the previous version of this comment got wrong by
# claiming both refuse their placeholder. Only gremote does: `load()` fails
# startup on `secret=change-me` with "still the shipped default; set one"
# (win/remote/config.cpp:210-214). GASTON.EXE has no such check; it simply
# dials out with whatever it was given and the bridge answers
# `[bad_secret] rejected`, which is a clear failure but not a refusal to
# start. The bridge holds the other end: it refuses to serve a non-loopback
# address with no GASTON_SECRET or with the shipped default
# (bridge/server.py). A release that booted straight into listening on
# 0.0.0.0 with a secret printed in a README would be worse than no release.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

VERSION=""
OUT="$HERE/build/release"
SKIP_BUILD=0

usage() {
  cat >&2 <<USAGE
usage: mkrelease.sh [--version X.Y.Z] [--out DIR] [--skip-build]

  --version    default: RC_VER_STR from win/app/resource.h, which is the
               version the binaries report in their own VERSIONINFO block.
               Passing one that disagrees with the header is refused: the
               zip name and the exe's About box must not be able to differ.
  --out        default: build/release
  --skip-build assume the binaries in build/win are current. For iterating
               on the packaging itself; never in CI.
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version)    VERSION="${2:?}"; shift 2 ;;
    --out)        OUT="${2:?}";     shift 2 ;;
    --skip-build) SKIP_BUILD=1;     shift ;;
    -h|--help)    usage ;;
    *) echo "mkrelease: unknown argument $1" >&2; usage ;;
  esac
done

# THE VERSION COMES FROM THE SOURCE, not from the tag and not from an
# argument. win/app/resource.h is what the linker stamps into the image, so
# it is the only spelling that cannot disagree with what a user reads in
# Help > About. The workflow checks its tag against this and stops if they
# differ, rather than renaming a file to match a tag.
HEADER_VER=$(sed -n 's/^#define RC_VER_STR  *"\([^"]*\)".*/\1/p' \
             "$HERE/win/app/resource.h")
if [ -z "$HEADER_VER" ]; then
  echo "mkrelease: no RC_VER_STR in win/app/resource.h" >&2
  exit 1
fi
if [ -z "$VERSION" ]; then
  VERSION="$HEADER_VER"
elif [ "$VERSION" != "$HEADER_VER" ]; then
  echo "mkrelease: --version $VERSION but win/app/resource.h says $HEADER_VER.
       The binaries would report one version and the zip would be named
       another. Change the header, or pass the version it already has." >&2
  exit 1
fi

echo "=== Gaston Code release $VERSION ==="
echo

if [ "$SKIP_BUILD" -eq 0 ]; then
  # The three product binaries and nothing else. `make -C win all` also
  # builds fifteen check binaries and the probes, which take minutes and
  # ship in nothing.
  echo "--- building gaston, gremote, gupdate ---"
  make -s -C "$HERE/win" gaston gremote gupdate
  echo

  # THE SIZE GATE IS PART OF THE RELEASE, not a separate opinion about it.
  # Every one of these six images has a committed-at-load budget measured on
  # a 32 MB machine, and gaston's slack is under 1% (510,217 of 513,273 on
  # 2026-10-24). A different Open Watcom build -- which CI will have, since
  # no dated release matches this tree's local toolchain byte for byte --
  # can cross that line, and a release that quietly shipped an image too big
  # for the machine it is for would be the worst possible way to find out.
  #
  # `check` also runs the NT-only import audit. A single NT-only static
  # import kills the image at load, before WinMain, with no log written and
  # nothing to debug -- so this is the one gate whose failure a user could
  # not diagnose for themselves.
  echo "--- make check: image caps and the NT-only import audit ---"
  make -s -C "$HERE/win" check
  echo
fi

BIN="$HERE/build/win"
for f in gaston.exe gremote.exe gupdate.exe; do
  [ -f "$BIN/$f" ] || { echo "mkrelease: $BIN/$f missing" >&2; exit 1; }
done

# ITS OWN TWO FILES, NOT THE DIRECTORY. This used to be `rm -rf "$OUT"`,
# which was right while mkrelease was the only thing writing there. It is
# not any more: mkwheel.sh and mkguide.py write the other two assets into
# the same directory, and no ordering of the three saves them -- run first,
# mkrelease deletes nothing and its sums cover two of four; run last, it
# deletes the other two. So it removes what it is about to replace and
# leaves everything else alone. `make release` is what runs all three and
# writes one SHA256SUMS over the result.
mkdir -p "$OUT"
rm -f "$OUT/gaston-$VERSION-win98.zip" "$OUT/gremote-$VERSION-win98.zip"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# A Win9x text file has CRLF line endings. This is not cosmetic: COMMAND.COM
# will not run a .BAT with bare LF, and GetPrivateProfileStringA -- which
# reads every .ini and view.tpl -- is the one API this tree has already been
# burned by (win/core/inifile.cpp). A release whose SETUP.BAT does not run
# fails on somebody else's machine, hours later, with no way to ask us.
crlf() { sed -i 's/\r$//; s/$/\r/' "$@"; }

# ---------------------------------------------------------------- gaston ---
G="$STAGE/gaston"
mkdir -p "$G"
cp "$BIN/gaston.exe"                  "$G/GASTON.EXE"
# Long names deliberately. The client opens these beside its own exe by
# these exact spellings; tools_detect.ini is 13.3 and has no 8.3 form at
# all, which is why the ISO carries it as TOOLSDET.INI and SETUP.BAT renames
# it. A zip has no such limit, so the names go in correct and nothing has to
# rename anything.
cp "$HERE/win/resources/view.css"     "$G/view.css"
cp "$HERE/win/resources/export.css"   "$G/export.css"
cp "$HERE/win/resources/view.tpl"     "$G/view.tpl"
cp "$HERE/win/resources/spin.gif"     "$G/spin.gif"
cp "$HERE/win/tools_detect.ini"       "$G/tools_detect.ini"

# NOT vm/gaston.ini, which is the QEMU development file: host=10.0.2.2 is
# user-mode networking's gateway and secret=dev-secret is published in this
# repository. A stranger's install must start from neither.
cat > "$G/gaston.ini" <<'INI'
; Gaston Code -- client configuration. Read from this file, beside
; GASTON.EXE, every time the program starts.
;
; EDIT THE THREE VALUES UNDER [bridge] BEFORE FIRST RUN. Until you do, the
; client will start, show its window, and fail to connect -- which is what
; it is supposed to do rather than guess.

[bridge]
; The modern machine running the bridge, as an address this Windows 98 box
; can actually reach. A name only works if this machine can resolve it, so
; an IP address is the safe answer.
host=192.168.1.2
port=7890
; MUST match GASTON_SECRET in the bridge's environment. Anything that is
; not this exact string is refused at the handshake.
secret=change-me

[ui]
; 1 scrolls the transcript as output arrives; 0 leaves it where you put it.
autoscroll=1

[sync]
; The directory on THIS machine that the bridge mirrors. It is created if it
; does not exist. Everything under it is visible to the agent.
root=C:\WORK
INI

cat > "$G/README.TXT" <<'INI'
Gaston Code @VERSION@ -- the Windows 98 client
=============================================

A native Windows 98 GUI client for Claude Code. This machine is both the
terminal you sit at and the workspace the agent operates on; a bridge
process on a modern Linux, Windows or container host runs Claude Code and
speaks to this program over one TCP connection.

WHAT YOU NEED BESIDES THIS ZIP
  * Windows 95 OSR2 or later. Built and measured on Windows 98 SE.
  * About 1 MB of disk, and the machine's own 16 MB or more of RAM. The
    client is measured on a 32 MB Pentium 200 MMX, which is the machine it
    is designed for.
  * A TCP/IP stack with an address, and the bridge reachable from it.
  * THE BRIDGE, running on another machine. It is a separate download:
    see the project page below. Without it this program has nothing to
    talk to.

INSTALL
  1. Make a directory and copy every file in this zip into it:

         MD C:\GASTON
         COPY *.* C:\GASTON

     Keep the long filenames. view.css, export.css, view.tpl and
     tools_detect.ini are opened by those exact spellings, and
     tools_detect.ini has no 8.3 form at all -- if your transfer shortened
     it to TOOLSDET.INI, rename it back.

  2. Edit C:\GASTON\gaston.ini. Set [bridge] host to the machine running
     the bridge, and [bridge] secret to the same string that machine has in
     GASTON_SECRET. The shipped values connect to nothing on purpose.

  3. Start the bridge on the other machine, then run:

         C:\GASTON\GASTON.EXE

     A Start menu shortcut is an ordinary shortcut; there is no installer
     and nothing is written outside this directory except the log.

FILES
  GASTON.EXE        the client. Reads gaston.ini from its own directory.
  gaston.ini        what you edit. See step 2.
  view.css          how the transcript looks -- colours, spacing, type.
  view.tpl          the markup each block is rendered through.
  export.css        extra rules applied only to a saved transcript.
  spin.gif          the "working" animation.
  tools_detect.ini  what to look for when reporting which tools this
                    machine has. Replaced on every upgrade; your own
                    additions go in tools.ini beside it, which is read
                    second so they win and which no upgrade touches.

IF IT DOES NOT CONNECT
  GASTON.LOG appears beside GASTON.EXE and names the reason. The usual
  three are a host this machine cannot route to, a secret that does not
  match the bridge's, and a bridge that is not running.

DRIVING THIS MACHINE REMOTELY
  There is a second program, gremote, which lets the bridge's machine
  control this one -- type at it, read its screen, start programs. It is a
  separate download because it is a separate decision: it opens a port on
  this machine. You do not need it to use the client.

Project page:  https://github.com/chibicitiberiu/gaston-code
Licence:       GNU AGPL v3 only. See LICENSE.TXT, and NOTICE.TXT for
               the two permissive components carried in with it.
INI
# @VERSION@ rather than $VERSION, because the heredoc above is quoted:
# prose with a backtick in it ran as a command and emptied a sentence
# before it was. Nothing in a README is interpreted now.
sed -i "s/@VERSION@/$VERSION/g" "$G/README.TXT"
cp "$HERE/LICENSE" "$G/LICENSE.TXT"
cp "$HERE/NOTICE"  "$G/NOTICE.TXT"
crlf "$G"/*.TXT "$G"/*.ini "$G"/*.css "$G"/*.tpl

# --------------------------------------------------------------- gremote ---
R="$STAGE/gremote"
mkdir -p "$R"
cp "$BIN/gremote.exe" "$R/GREMOTE.EXE"
# GUPDATE.EXE must sit beside the running image or UPDATE is refused with
# `no_updater` (win/remote/control.cpp). Shipping gremote without it means
# the only way to install a new build on a machine in another room is to
# walk there, which is the problem gremote exists to solve.
cp "$BIN/gupdate.exe" "$R/GUPDATE.EXE"

cat > "$R/gremote.ini" <<'INI'
; gremote -- server configuration. Read from this file, beside GREMOTE.EXE,
; at every start.
;
; IT WILL NOT START UNTIL YOU CHANGE THE SECRET. That is deliberate: a
; default that worked would mean every machine nobody configured comes up
; listening with a secret published in a document.

[server]
; Which interface to listen on. 0.0.0.0 is every interface; `allow` below is
; what narrows who may connect, not this.
bind=0.0.0.0
port=7401
; CHANGE THIS. Startup fails with "[server] secret: still the shipped
; default; set one" until you do. Use something long and random -- it is
; sent in clear over the LAN, and presenting it grants everything in the
; [ui] section below.
secret=change-me
; Who may connect, as one address or one address/prefix. EMPTY MEANS
; ANYBODY who can reach the port, which on a LAN is every machine on the
; segment. Narrow it to the host running the bridge:
;     allow=192.168.1.2
; or to its subnet:
;     allow=192.168.1.0/24
allow=
; Drop a connection that has gone quiet for this long. 0 disables it.
heartbeat_timeout_s=30

[exec]
max_timeout_ms=120000
max_output_kb=1024

[ui]
; THIS SECTION DECIDES WHAT A CALLER CAN DO TO THIS MACHINE.
;   2  the whole desktop lane: read the UI tree, take screenshots, AND send
;      keystrokes and mouse clicks
;   1  read-only -- trees and screenshots, no input
;   0  off; every ui request is refused
; 2 is the shipped default because it is what the tool is for. If this
; machine has anything on it you would not hand to whoever holds the
; secret, 1 or 0 is the honest setting.
automation=2
; Show the "Gaston remote is controlling this desktop" indicator at the top
; of the screen while a caller is driving. 0 hides it. Leave it at 1: the
; person at the keyboard should be able to see that the machine is not
; theirs, and stop it.
indicator=1
; 0 skips oleacc.dll even when present; the walk falls back to the window
; tree. On a slow machine MSAA costs about 3.4x (measured on a Pentium 200),
; so 0 is a reasonable trade if names and roles are not needed.
msaa=1
; Ceilings a request is clamped to, not fixed values.
max_tree_nodes=4000
call_timeout_ms=2000
script_ms=600000
step_ms=120000

; [power]
; prevent_sleep=1 asks Windows to refuse standby while gremote is up, so a
; machine in another room does not suspend mid-session. Off by default,
; because turning it on changes how the hardware behaves.
INI

cat > "$R/GREMOTE.REG" <<'REG'
REGEDIT4

[HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\RunServices]
"GastonRemote"="C:\GREMOTE\GREMOTE.EXE"
REG

cat > "$R/README.TXT" <<'INI'
gremote @VERSION@ -- remote control for a Windows 98 machine
===========================================================

A server for Windows 95/98/Me. It lets another machine on your network run
programs here, read and write files, take screenshots, walk the window tree
and send keystrokes and clicks. It is how a Windows 98 box in another room
gets driven without somebody standing at it.

READ THIS BEFORE INSTALLING
  gremote opens a TCP port and hands whoever presents the right secret the
  ability to run programs, read any file this machine can read, and type at
  the desktop. The secret is sent IN CLEAR. There is no encryption and no
  user accounts -- Windows 98 has none to use.

  So: a trusted network segment, an `allow` line naming the one host that
  should connect, and a secret you generated rather than one you invented.
  This is the right tool for a lab machine or a retro box on its own VLAN,
  and the wrong tool for anything else.

WHAT YOU NEED
  * Windows 95 OSR2 or later. Built and measured on Windows 98 SE.
  * A TCP/IP stack with an address.
  * About 400 KB of disk.

INSTALL
  1. Copy both programs and the ini into a directory of their own:

         MD C:\GREMOTE
         COPY *.* C:\GREMOTE

     GUPDATE.EXE must stay beside GREMOTE.EXE. It is what installs a new
     build sent over the network; without it in the same directory, that is
     refused and a new version means a walk to this machine.

  2. Edit C:\GREMOTE\gremote.ini. At minimum set [server] secret to
     something long and random, and [server] allow to the address of the
     one machine that should be able to connect. GREMOTE.EXE REFUSES TO
     START while the secret is the shipped placeholder, and says so in
     GREMOTE.LOG.

  3. Try it in the foreground first:

         C:\GREMOTE\GREMOTE.EXE

     GREMOTE.LOG appears beside it. The line to look for is
     "listen: on 0.0.0.0:7401". An icon appears in the notification area;
     right-click it to see what is connected and to quit.

  4. Only once step 3 works, make it start at every boot:

         REGEDIT /S C:\GREMOTE\GREMOTE.REG

     This is a RunServices entry, which runs BEFORE anyone logs on -- that
     is what makes the machine reachable after a reboot rather than after
     somebody walks over and logs in. It is deliberately not automatic,
     because it opens a port on this machine.

     The .REG names C:\GREMOTE\GREMOTE.EXE. IF YOU INSTALLED SOMEWHERE
     ELSE, EDIT IT FIRST. An entry pointing at a directory the program is
     not in fails silently at every boot, forever: this exact mistake went
     unnoticed for weeks on the authors' own machine, because every
     apparent autostart turned out to be somebody at the keyboard.

  5. Verify it really did autostart: reboot, and WITHOUT LOGGING ON, check
     from the other machine that the port answers. Then read GREMOTE.LOG.
     Before any shell exists it should say
     "tray: no notification area yet (no shell is running)" and then
     "listen: on 0.0.0.0:7401". If the port only answers after you log on,
     step 4 did not take.

FILES
  GREMOTE.EXE   the server.
  GUPDATE.EXE   installs a build sent over the network. Keep it alongside.
  gremote.ini   what you edit. See step 2.
  GREMOTE.REG   the autostart entry. See step 4.

THERE IS NO REMOTE SHUTDOWN, ON PURPOSE
  Nothing a caller can send stops gremote. A remote quit on a machine whose
  autostart runs at boot would strand it until somebody walked over, so the
  only ways to stop it are the tray icon's Exit and a reboot.

Project page:  https://github.com/chibicitiberiu/gaston-code
Licence:       GNU AGPL v3 only. See LICENSE.TXT, and NOTICE.TXT for
               the two permissive components carried in with it.
INI
sed -i "s/@VERSION@/$VERSION/g" "$R/README.TXT"
cp "$HERE/LICENSE" "$R/LICENSE.TXT"
cp "$HERE/NOTICE"  "$R/NOTICE.TXT"
crlf "$R"/*.TXT "$R"/*.ini "$R"/*.REG

# ------------------------------------------------------------------ zips ---
# -X drops the extra attributes that make a zip differ between two machines
# for no reason anybody can see. The file list is named rather than
# wildcarded so a file that failed to stage is a missing-file error here and
# not a short zip that nobody notices.
echo "--- packaging ---"
GASTON_FILES=(GASTON.EXE gaston.ini view.css export.css view.tpl spin.gif
              tools_detect.ini README.TXT LICENSE.TXT NOTICE.TXT)
GREMOTE_FILES=(GREMOTE.EXE GUPDATE.EXE gremote.ini GREMOTE.REG
               README.TXT LICENSE.TXT NOTICE.TXT)

( cd "$G" && zip -q -X "$OUT/gaston-$VERSION-win98.zip"   "${GASTON_FILES[@]}" )
( cd "$R" && zip -q -X "$OUT/gremote-$VERSION-win98.zip" "${GREMOTE_FILES[@]}" )

# NAMED, not globbed. `"$OUT"/*.zip` would print the guide zip's contents
# as though this script had built it.
for z in "$OUT/gaston-$VERSION-win98.zip" "$OUT/gremote-$VERSION-win98.zip"; do
  echo
  echo "$(basename "$z")  --  $(stat -c%s "$z") bytes"
  unzip -l "$z" | sed -n '4,$p' | head -n -2 | sed 's/^/    /'
done

# NO SHA256SUMS HERE. It used to write one over `*.zip`, which after the
# wheel joined the release would have hashed the guide zip and silently
# MISSED the wheel -- `.whl` is not `*.zip`. A sums file that covers some
# of the assets is worse than none, because it looks complete. `make
# release` writes one over everything, once, last.
echo
echo "guest $VERSION in $OUT"
