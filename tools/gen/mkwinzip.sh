#!/usr/bin/env bash
# mkwinzip.sh -- the bridge as a Windows zip, built on Linux.
#
# WHY NOT PYINSTALLER ON A WINDOWS RUNNER. Because none is needed. The
# bridge is pure Python plus wheels, so the whole artefact is CPython's own
# embeddable distribution with the wheels unpacked beside it -- and
# `pip install --target --platform win_amd64 --only-binary :all:` fetches
# the Windows wheels from a Linux machine perfectly well. That keeps the
# whole release buildable and testable in one place, on one runner, and
# means a Windows build cannot fail for reasons nobody here can reproduce.
#
# The cost is that the result is not ONE file, as PyInstaller's would be. It
# is a directory a person unzips and a .cmd they run. For a program whose
# first instruction is "edit the secret and pick a port", that is the right
# shape anyway.
#
# IT CARRIES CLAUDE CODE, and not because this script puts it there.
# claude-agent-sdk ships one wheel per platform and the win_amd64 one is
# 98 MB, nearly all of which is claude.exe as package data at
# claude_agent_sdk/_bundled/claude.exe -- 231 MB unpacked, and the reason
# this zip is 130 MB. The SDK's `_find_bundled_cli()` is checked BEFORE
# shutil.which, so that copy is the one it runs.
#
# An earlier version of this launcher refused to start unless a claude.exe
# was on PATH, with a careful explanation about npm's claude.cmd shim being
# unspawnable on Windows. All of that is true of the SDK's FALLBACK path and
# none of it applies here, because the fallback is never reached. The guard
# would have turned a working download into a refusal, on the machine it
# exists for. It is gone, and the build asserts the bundled binary instead.
#
# EMBEDDABLE PYTHON HAS NO PIP AND NO SITE. That is the point of it: a
# python313._pth file replaces site initialisation, so the wheels have to be
# installed into a directory the ._pth names. Hence --target and the extra
# line appended to it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Pinned, and the same 3.13.17 the container image and the AppImage use.
PY_FULL=3.13.17
PY_TAG=313
OUT="$HERE/build/release"

usage() {
  cat >&2 <<USAGE
usage: mkwinzip.sh [--out DIR] [--python X.Y.Z]

  --out      default: build/release
  --python   default: $PY_FULL -- keep it the same as docker/Dockerfile's
                      and mkappimage.sh's
USAGE
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --out)     OUT="${2:?}";     shift 2 ;;
    --python)  PY_FULL="${2:?}"; PY_TAG="$(echo "$PY_FULL" | cut -d. -f1,2 | tr -d .)"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "mkwinzip: unknown argument $1" >&2; usage ;;
  esac
done

VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$HERE/pyproject.toml" | head -1)
[ -n "$VERSION" ] || { echo "mkwinzip: no version in pyproject.toml" >&2; exit 1; }

echo "=== gaston-bridge $VERSION, Windows x64 on embeddable Python $PY_FULL ==="

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
APP="$WORK/gaston-bridge"
mkdir -p "$APP" "$OUT"

echo "--- build tools ---"
python3 -m venv "$WORK/tools"
"$WORK/tools/bin/pip" install --quiet --upgrade pip build

echo "--- wheel ---"
"$WORK/tools/bin/python" -m build --wheel --outdir "$WORK/wheel" "$HERE" >/dev/null
WHEEL=$(echo "$WORK"/wheel/*.whl)
[ -f "$WHEEL" ] || { echo "mkwinzip: no wheel built" >&2; exit 1; }
echo "    $(basename "$WHEEL")"

echo "--- embeddable CPython $PY_FULL ---"
EMBED="python-$PY_FULL-embed-amd64.zip"
curl -fsSL -o "$WORK/$EMBED" \
     "https://www.python.org/ftp/python/$PY_FULL/$EMBED"
unzip -q "$WORK/$EMBED" -d "$APP/python"
echo "    $(stat -c%s "$WORK/$EMBED") bytes unpacked"

echo "--- Windows wheels, fetched from here ---"
# --platform/--only-binary is what makes this work off a Linux box: pip is
# told which wheels to pick rather than building anything, so nothing is
# compiled for the wrong machine. A dependency with no win_amd64 wheel would
# fail HERE, loudly, which is the right place for it.
#
# --target rather than a venv: the embeddable distribution has no site
# machinery, so packages go in a plain directory named by the ._pth below.
"$WORK/tools/bin/pip" install --quiet \
    --target "$APP/python/lib" \
    --platform win_amd64 \
    --python-version "$(echo "$PY_FULL" | cut -d. -f1,2)" \
    --only-binary :all: \
    --no-compile \
    -r "$HERE/requirements.txt" "$WHEEL"

# The embeddable distribution looks for python<tag>._pth and uses it INSTEAD
# of the usual sys.path construction. Nothing is importable from `lib`
# until it is named here.
cat >> "$APP/python/python$PY_TAG._pth" <<PTH
lib
PTH

# Console scripts pip wrote into lib/bin are useless: their shebangs point
# at the build machine's python. The launcher below is the entry point.
rm -rf "$APP/python/lib/bin"

cat > "$APP/gaston-bridge.cmd" <<'CMD'
@ECHO OFF
REM The bridge. Everything after this line is passed through, so:
REM     gaston-bridge.cmd --host 192.168.1.2 --cwd C:\work
REM
REM Set GASTON_SECRET first -- see README.TXT. Without it the bridge runs
REM with a published development secret and warns about it.
REM
REM Claude Code needs no separate install: the agent SDK's Windows wheel
REM carries claude.exe inside it and prefers that copy to anything on PATH.
REM Credentials are still yours to provide -- run `claude` once to sign in,
REM or set ANTHROPIC_API_KEY.
REM
REM GASTON_PROG only changes what argparse calls this program in its usage
REM line: without it, `-m bridge.server` makes that "server.py".
SETLOCAL
SET "GASTON_PROG=gaston-bridge"
SET "HERE=%~dp0"
"%HERE%python\python.exe" -m bridge.server %*
ENDLOCAL
CMD

cat > "$APP/README.TXT" <<'README'
Gaston Code bridge @VERSION@ -- Windows x64
===========================================

The modern-host half of Gaston Code. It runs Claude Code and speaks the
wire protocol to a Windows 98 machine on your network over one TCP
connection. The Windows 98 side is a separate download.

This zip carries its own Python. It does not use, need or touch any Python
you already have, and it installs nothing: unzip it anywhere and delete the
directory to uninstall.

Claude Code itself is already inside this zip -- the agent SDK's Windows
wheel carries claude.exe and uses that copy in preference to anything on
your PATH -- so there is nothing to install. Two things are still yours to
set up.

BEFORE IT WILL WORK
  1. GIVE IT CREDENTIALS. Either set ANTHROPIC_API_KEY in the environment:

         setx ANTHROPIC_API_KEY "sk-ant-..."

     or, if you have Claude Code installed separately and signed in
     already, that login is in %USERPROFILE%\.claude and this will find it.

  2. SET THE SHARED SECRET. It has to match the one in the Windows 98
     machine's gaston.ini:

         setx GASTON_SECRET "something-long-and-random"

     Open a new console afterwards, because setx affects new ones only.
     Without it the bridge starts with a development secret published in
     the project's source and warns that it has.

RUNNING IT
       gaston-bridge.cmd --host 192.168.1.2 --cwd C:\work

  --host    the address on THIS machine that the Windows 98 box should
            connect to. Not 0.0.0.0 unless you mean it: the port lets
            whoever presents the secret approve the agent's permission
            prompts and feed it fabricated tool results.
  --port    default 7890. Must match the client's gaston.ini.
  --cwd     the directory the agent works in. Also mirrored to the guest
            unless you pass --sync-dir or --no-sync.

  gaston-bridge.cmd --help lists the rest.

THEN, ON THE WINDOWS 98 MACHINE
  Install the client from gaston-<version>-win98.zip, point its gaston.ini
  at this machine's address and port, give it the same secret, and run
  GASTON.EXE.

WHAT IS IN HERE
  gaston-bridge.cmd   the launcher. Run this.
  python\             CPython @PYFULL@, embeddable distribution. Not a
                      Python you have; nothing outside this folder is
                      touched, and deleting the folder uninstalls it.
  python\lib\         the bridge, its dependencies, and Claude Code --
                      claude.exe lives in
                      lib\claude_agent_sdk\_bundled\ and is most of the
                      130 MB.
  README.TXT          this file.
  LICENSE.TXT         GNU AGPL v3 only. NOTICE.TXT has the permissive
                      components carried in with it.

Project page:  https://github.com/chibicitiberiu/gaston-code
README
sed -i "s/@VERSION@/$VERSION/g; s/@PYFULL@/$PY_FULL/g" "$APP/README.TXT"
cp "$HERE/LICENSE" "$APP/LICENSE.TXT"
cp "$HERE/NOTICE"  "$APP/NOTICE.TXT"
# CRLF on the text a Windows user opens in Notepad, and on the .cmd, which
# is the same lesson the Win98 side learned the hard way.
sed -i 's/\r$//; s/$/\r/' "$APP/README.TXT" "$APP/LICENSE.TXT" \
                          "$APP/NOTICE.TXT" "$APP/gaston-bridge.cmd"

echo "--- packaging ---"
DEST="$OUT/gaston-bridge-$VERSION-windows-x64.zip"
rm -f "$DEST"
( cd "$WORK" && zip -q -r -X "$DEST" gaston-bridge )

echo
echo "$(basename "$DEST")  --  $(stat -c%s "$DEST") bytes"
echo "    $(unzip -l "$DEST" | tail -1 | awk '{print $2}') files"
echo

# The one thing that can be checked without Windows: that the import graph
# the zip contains is complete and that every native wheel in it really is
# win_amd64. A linux wheel in here would run on nothing and the zip would
# look perfectly fine.
echo "--- the SDK's bundled claude.exe is really in there ---"
BUNDLED="$APP/python/lib/claude_agent_sdk/_bundled/claude.exe"
if [ ! -f "$BUNDLED" ]; then
  echo "mkwinzip: no $BUNDLED. The win_amd64 wheel of claude-agent-sdk is
       supposed to carry Claude Code as package data, and the launcher and
       the README both now tell the user there is nothing to install. If
       that has changed upstream, this artefact needs a separately
       installed CLI and both of those have to say so again." >&2
  exit 1
fi
echo "    ok: $(stat -c%s "$BUNDLED") bytes of claude.exe"

echo "--- checking every binary module is win_amd64 ---"
BAD=0
while IFS= read -r so; do
  echo "    NOT a Windows module: $so" >&2
  BAD=1
done < <(cd "$APP/python/lib" && find . -name "*.so" -o -name "*.dylib")
PYDS=$(cd "$APP/python/lib" && find . -name "*.pyd" | wc -l)
[ "$BAD" -eq 0 ] || { echo "mkwinzip: non-Windows binaries in the zip" >&2; exit 1; }
echo "    ok: $PYDS .pyd modules, no .so and no .dylib"

echo
echo "windows zip $VERSION in $OUT"
