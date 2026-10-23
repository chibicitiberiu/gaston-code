#!/usr/bin/env bash
# mkappimage.sh -- the bridge as a single-file Linux executable.
#
# WHAT AN APPIMAGE BUYS HERE. The bridge needs a current Python and about
# thirty wheels. An AppImage carries both, so somebody running a stable
# distribution with Python 3.9 can use it without a venv, a package index
# or any opinion about how Python should be installed on their machine.
#
# IT CARRIES CLAUDE CODE, and not because this script puts it there.
# claude-agent-sdk ships one wheel per platform and each carries the CLI as
# package data at claude_agent_sdk/_bundled/claude -- 228 MB unpacked, which
# is most of why a bridge AppImage is 133 MB of squashfs. The SDK checks
# that copy BEFORE shutil.which, so it is the one that runs.
#
# An earlier version of this script had an entrypoint that refused to start
# when no `claude` was on PATH, on the stated premise that the SDK does not
# contain it. That premise was wrong, and the guard was therefore a bug: it
# would have refused to run on exactly the machine the AppImage exists for
# -- one with no Claude Code installed -- while carrying its own perfectly
# good copy. Tested and believed before it was checked, which is the whole
# reason this project runs things instead of reasoning about them.
#
# The one install that genuinely has no bundled CLI is from the sdist
# (claude_agent_sdk-<v>.tar.gz carries no binary). Nothing here installs
# that way, and the check below asserts it at build time rather than
# trusting it.
#
# PYTHON 3.13.17, the same version the container image pins, from the same
# manylinux2014 tree python-appimage pulls. Two artefacts of one release
# running two different interpreters is a difference that costs an evening
# the first time it matters.
#
# NEEDS A CONTAINER RUNTIME. python-appimage pulls and extracts the
# manylinux2014 image to get its Python. That is why this is a script run on
# a machine (or a CI runner) with docker available, and not a pip install.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PYVER=3.13
LINUXTAG=manylinux2014_x86_64
OUT="$HERE/build/release"

usage() {
  cat >&2 <<USAGE
usage: mkappimage.sh [--out DIR] [--python X.Y] [--linux-tag TAG]

  --out         default: build/release
  --python      default: $PYVER -- keep it the same as docker/Dockerfile's
  --linux-tag   default: $LINUXTAG
USAGE
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --out)        OUT="${2:?}";      shift 2 ;;
    --python)     PYVER="${2:?}";    shift 2 ;;
    --linux-tag)  LINUXTAG="${2:?}"; shift 2 ;;
    -h|--help)    usage ;;
    *) echo "mkappimage: unknown argument $1" >&2; usage ;;
  esac
done

# The host version, from pyproject.toml. The guest's lives in
# win/app/resource.h and mkrelease.sh is what checks it; these two are
# released together and carry the same number.
VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$HERE/pyproject.toml" | head -1)
[ -n "$VERSION" ] || { echo "mkappimage: no version in pyproject.toml" >&2; exit 1; }

echo "=== gaston-bridge $VERSION, AppImage on Python $PYVER ==="

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
RECIPE="$WORK/recipe"
mkdir -p "$RECIPE" "$OUT"

# A venv of its own for the build tools. Not the project's .venv: this
# installs python-appimage and `build`, which are release machinery and have
# no business in the environment the tests run in.
echo "--- build tools ---"
python3 -m venv "$WORK/tools"
"$WORK/tools/bin/pip" install --quiet --upgrade pip build python-appimage

echo "--- wheel ---"
"$WORK/tools/bin/python" -m build --wheel --outdir "$WORK/wheel" "$HERE" >/dev/null
WHEEL=$(echo "$WORK"/wheel/*.whl)
[ -f "$WHEEL" ] || { echo "mkappimage: no wheel built" >&2; exit 1; }
echo "    $(basename "$WHEEL")"

# The pinned transitive closure FIRST, our wheel LAST, and the order is the
# whole mechanism.
#
# python-appimage runs ONE `pip install` PER LINE of this file (see
# commands/build/app.py), which rules out putting a bare `--no-deps` on a
# line of its own -- pip is handed it as the requirement and says "you must
# give at least one requirement to install". It does not need one anyway:
# pip's default upgrade strategy is only-if-needed, and the wheel asks for
# `claude-agent-sdk>=0.2.153` and `pillow>=12.0`, which the pinned
# `==0.2.153` and `==12.3.0` installed above already satisfy. So nothing is
# re-resolved and nothing drifts, as long as the wheel goes last.
{
  cat "$HERE/requirements.txt"
  echo
  echo "# The bridge itself, built from this tree a moment ago, and last so"
  echo "# that its own version ranges are already satisfied by the pins."
  echo "$WHEEL"
} > "$RECIPE/requirements.txt"

# THIS FILE IS INLINED INTO AppRun, not executed as a script of its own:
# python-appimage renders data/apprun.sh with `{{ entrypoint }}` set to the
# contents of this file and `{{ shebang }}` of its own. So no shebang here,
# and the placeholder is `{{ python-executable }}` -- which expands to
# ${APPDIR}/usr/bin/python3.13. The `{{ python }}` in python-appimage's own
# bundled entrypoint.sh belongs to a different code path (the plain-Python
# image) and expands to just "python" here, which is a path that does not
# exist and an AppImage that builds and cannot start.
#
# `-m bridge.server` rather than the gaston-bridge console script, because
# a console script carries a shebang written at install time and `-m`
# cannot be wrong about where the interpreter is.
cat > "$RECIPE/entrypoint.sh" <<'ENTRY'
# So argparse says "usage: gaston-bridge" rather than "usage: server.py",
# which is what `-m bridge.server` gives it from sys.argv[0].
export GASTON_PROG=gaston-bridge
exec {{ python-executable }} -m bridge.server "$@"
ENTRY

cat > "$RECIPE/gaston-bridge.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
# NO SPACES. appimagetool names the output file after this field, not after
# python-appimage's -n, so "Gaston Code bridge" produced
# "Gaston Code bridge-x86_64.AppImage" and then failed copying it. The
# readable name lives in GenericName, and this is a Terminal=true program
# that nobody launches from a menu anyway.
Name=gaston-bridge
GenericName=Gaston Code bridge
Exec=gaston-bridge
Comment=Runs Claude Code for a Windows 98 client on the network
Icon=gaston-bridge
Categories=Development;
Terminal=true
DESKTOP

# The icon is the client's own, which is a 16x16 and a 32x32 .ico drawn
# pixel by pixel in tools/gen/mkicon.py. Scaled with NEAREST deliberately:
# a smooth resample of pixel art gives a blurry mess, and the blockiness is
# the point of this icon.
"$WORK/tools/bin/pip" install --quiet pillow
"$WORK/tools/bin/python" - "$HERE/win/resources/gaston.ico" \
                           "$RECIPE/gaston-bridge.png" <<'PY'
import sys

from PIL import Image

src, dst = sys.argv[1], sys.argv[2]
with Image.open(src) as ico:
    # A .ico is a container of frames, and PIL opens the LARGEST one: for
    # gaston.ico, which carries 16x16 and 32x32, `size` is already (32, 32)
    # and `info["sizes"]` lists both. Pillow 12 has no IcoImageFile.sizes()
    # method, whatever an older recipe for this might say.
    best = ico.size
    frame = ico.convert("RGBA")
frame.resize((256, 256), Image.NEAREST).save(dst)
print("    icon %dx%d -> 256x256 (nearest, it is pixel art)" % best)
PY

# THE LICENCE HAS TO TRAVEL WITH IT. This is AGPLv3 code, and section 4
# requires conveying the licence along with the work -- a single-file
# artefact with no licence in it is a compliance hole, not an untidiness.
# `-x` is python-appimage's way to put extra files under $APPDIR; the two
# land beside the AppRun where `--appimage-extract` will show them.
cp "$HERE/LICENSE" "$WORK/LICENSE"
cp "$HERE/NOTICE"  "$WORK/NOTICE"

echo "--- AppImage ---"
( cd "$WORK" && "$WORK/tools/bin/python-appimage" build app \
    -p "$PYVER" -l "$LINUXTAG" -n "gaston-bridge" \
    -x LICENSE NOTICE -- "$RECIPE" )

BUILT=$(echo "$WORK"/gaston-bridge*.AppImage)
[ -f "$BUILT" ] || { echo "mkappimage: python-appimage produced nothing" >&2; exit 1; }

DEST="$OUT/gaston-bridge-$VERSION-x86_64.AppImage"
mv "$BUILT" "$DEST"
chmod +x "$DEST"

echo
echo "$(basename "$DEST")  --  $(stat -c%s "$DEST") bytes"
echo
# It must at least answer --help. An AppImage that builds and cannot start
# is the normal failure here: a missing shared library shows up at run time
# and never at build time.
echo "--- smoke test: --help, under env -i so nothing leaks in ---"
# env -i on purpose. Run with this shell's environment, an AppImage can pass
# by using the developer's own PATH, HOME and PYTHON* variables; stripped,
# what answers is only what is inside the file. That is also what proves the
# bundled CLI claim rather than assuming it.
if ! env -i HOME=/nonexistent PATH=/usr/bin:/bin "$DEST" --help >/dev/null 2>&1; then
  echo "mkappimage: the AppImage was built but will not run:" >&2
  env -i HOME=/nonexistent PATH=/usr/bin:/bin "$DEST" --help 2>&1 | head -20 >&2
  exit 1
fi
echo "    ok, it runs and parses arguments with an empty environment"

# WHAT IS ACTUALLY INSIDE THE FILE, read out of the squashfs once and then
# searched. Asked of the running program instead, a check could be answered
# by something the build machine happens to have.
#
# -o THE OFFSET, which is the whole trick. An AppImage is a small ELF
# question is what this FILE contains, and a running copy could answer from
# something it found on the build machine.
#
# -o THE OFFSET, which is the whole trick. An AppImage is a small ELF
# runtime with the squashfs appended, so `unsquashfs -l` on it alone finds
# no superblock and fails -- and a `grep -q` on that empty output reads
# exactly like "the file is missing". It said so once, about a file that was
# there. The image prints its own offset.
#
# AND NOT `| grep -q`. Under `set -o pipefail` -- which is on at the top of
# this file -- grep -q exits the moment it matches, unsquashfs dies of
# SIGPIPE writing the rest of its 30,000 lines, and the pipeline reports
# failure for a listing that contained exactly what was asked for. The
# second false alarm about the same present file, from the same check, for
# a completely different reason. So the listing is captured first and
# searched after.
OFFSET=$("$DEST" --appimage-offset)
unsquashfs -o "$OFFSET" -l "$DEST" > "$WORK/listing.txt" 2>/dev/null

echo "--- the licence travelled with it ---"
# AGPLv3 section 4 requires conveying the licence along with the work, so a
# single-file artefact without it is a compliance hole rather than an
# untidiness. `--appimage-extract` is how a user gets at them.
for f in LICENSE NOTICE; do
  if ! grep -qE "squashfs-root/$f\$" "$WORK/listing.txt"; then
    echo "mkappimage: $f is not inside the AppImage. It is AGPLv3 code and
       the licence has to travel with it." >&2
    exit 1
  fi
done
echo "    ok: LICENSE and NOTICE are both in there"

echo "--- the SDK's bundled Claude Code is really in there ---"
if ! grep -q "claude_agent_sdk/_bundled/claude" "$WORK/listing.txt"; then
  echo "mkappimage: no bundled Claude Code inside the AppImage. The SDK's
       platform wheel is supposed to carry one at
       claude_agent_sdk/_bundled/claude, and without it this artefact needs
       a separately installed CLI that nothing told the user to install." >&2
  exit 1
fi
echo "    ok: claude_agent_sdk/_bundled/claude is present"
echo
echo "appimage $VERSION in $OUT"
