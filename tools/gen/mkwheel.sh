#!/usr/bin/env bash
# mkwheel.sh -- the bridge, as one wheel that runs anywhere.
#
# WHAT REPLACED WHAT. This used to be three scripts: a Linux AppImage, a
# Windows zip carrying its own CPython, and a container image. All three
# existed because claude-agent-sdk's platform wheels bundle Claude Code
# itself -- 219 MB of the SDK's 220 -- so "install the bridge" meant moving
# a quarter of a gigabyte and the only question was which container to move
# it in.
#
# The SDK's sdist carries no binary. Installed from source it falls through
# to the CLI the user already has, which makes the bridge 477 KB of pure
# Python and makes one `py3-none-any` wheel cover Linux, macOS and Windows
# -- more ground than the three artefacts covered between them, since macOS
# never had one at all. The cost is a prerequisite, and docs/GUIDE.md is
# where that is explained.
#
# `python -m build` WITH NO `--wheel`, AND THAT IS THE WHOLE POINT OF THE
# LINE. `--wheel` builds in place and does not clear `build/lib`, so a file
# deleted from bridge/ keeps shipping. Reproduced rather than feared: a
# planted build/lib/bridge/zz_stale_module.py came back inside the wheel,
# and `build/` is a live directory in this repo. Without the flag, `build`
# makes an sdist first and builds the wheel from a fresh extraction of it,
# which cannot carry anything the source tree does not have. Same bytes
# otherwise.
#
# The .tar.gz it also emits is a build artefact, not a release asset: the
# upload glob is *.whl.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

OUT="$HERE/build/release"

usage() {
  cat >&2 <<USAGE
usage: mkwheel.sh [--out DIR]

  --out   default: build/release
USAGE
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --out)     OUT="${2:?}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "mkwheel: unknown argument $1" >&2; usage ;;
  esac
done

VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$HERE/pyproject.toml" | head -1)
[ -n "$VERSION" ] || { echo "mkwheel: no version in pyproject.toml" >&2; exit 1; }

echo "=== gaston-code-bridge $VERSION, wheel ==="

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

# Its own venv for the build tools, not the project's: `build` is release
# machinery and the environment the tests run in should not acquire it as a
# side effect of somebody packaging once.
python3 -m venv "$WORK/tools"
"$WORK/tools/bin/pip" install --quiet --upgrade pip build

echo "--- building (via sdist, so build/lib cannot leak in) ---"
"$WORK/tools/bin/python" -m build --outdir "$WORK/dist" "$HERE" >/dev/null

WHEEL=$(echo "$WORK"/dist/*.whl)
[ -f "$WHEEL" ] || { echo "mkwheel: no wheel built" >&2; exit 1; }

# --- what has to be true of it ---------------------------------------------
#
# Each of these has a failure behind it rather than a hope. There is NO
# "contains no _bundled" assertion: nothing in this source tree could put
# claude_agent_sdk's package data into this project's own wheel, so it would
# test the test. The property worth checking belongs to the INSTALLED
# environment, and the release verification checks it there.
echo "--- checking ---"
LIST=$(unzip -Z1 "$WHEEL")

case "$(basename "$WHEEL")" in
  *-py3-none-any.whl) ;;
  *) echo "mkwheel: $(basename "$WHEEL") is not py3-none-any. One wheel for
       every host is the reason this replaced three artefacts." >&2
     exit 1 ;;
esac
echo "    py3-none-any"

# RUNTIME DATA, resolved by path. bridge/agent/profiles.py and prompts.py
# both read `Path(__file__).parent.parent / <dir>`, so these have to be
# installed beside the package. Missing, the failure is an empty tool
# inventory and a silent prompt -- not an import error anybody would notice.
for pair in "profiles 15" "prompts 4"; do
  set -- $pair
  n=$(echo "$LIST" | grep -c "^bridge/$1/.*\.toml$" || true)
  [ "$n" = "$2" ] || { echo "mkwheel: $n bridge/$1/*.toml in the wheel, expected $2" >&2; exit 1; }
  echo "    bridge/$1: $2 toml"
done

# AGPLv3 section 4 wants the licence conveyed with the work. setuptools >=77
# reads pyproject's PEP 639 `license-files` and puts them under
# *.dist-info/licenses/ -- NOT at the wheel root, which is where an earlier
# version of this check looked and would have passed on nothing.
for f in LICENSE NOTICE; do
  echo "$LIST" | grep -qE "^[^/]+\.dist-info/licenses/$f$" || {
    echo "mkwheel: $f is not under *.dist-info/licenses/ in the wheel. It is
       AGPLv3 code and the licence has to travel with it." >&2
    exit 1; }
done
echo "    LICENSE and NOTICE under dist-info/licenses/"

# The development tools are deliberately NOT installed (see pyproject's
# `packages`). fakegui is the client for the handshake that rewrites a
# bridge's own permission policy; gremote is a host-side driver a bridge
# user has no call for and a top-level name that could collide.
for d in bridge/devtools gremote; do
  echo "$LIST" | grep -q "^$d/" && {
    echo "mkwheel: $d is in the wheel and should not be" >&2; exit 1; }
done
echo "    no devtools, no gremote"

# --- it has to install and answer ------------------------------------------
#
# Built is not installed. A wheel that imports on this box out of the source
# directory proves nothing about one unpacked somewhere else, and the two
# path lookups above are exactly the kind that pass in a checkout.
echo "--- installing it somewhere else and asking it for --help ---"
python3 -m venv "$WORK/try"
PIP_NO_BINARY=claude-agent-sdk "$WORK/try/bin/pip" install --quiet "$WHEEL"
( cd "$WORK" && "$WORK/try/bin/gaston-bridge" --help >/dev/null )
echo "    gaston-bridge --help answers"

# The SDK installed beside it must be the source build, or this artefact
# needs a 219 MB binary nobody downloaded. Asked of the install rather than
# assumed from the flag.
"$WORK/try/bin/python" - <<'PY'
import pathlib
import sys

import claude_agent_sdk

bundled = pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled"
stray = [p.name for p in bundled.glob("*") if p.name != ".gitignore"]
if stray:
    sys.exit("mkwheel: the installed SDK still carries %s. The install must "
             "use PIP_NO_BINARY=claude-agent-sdk, or every user downloads "
             "219 MB of CLI that this release says it does not ship." % stray)
print("    claude_agent_sdk is the sdist build, no bundled CLI")
PY

cp "$WHEEL" "$OUT/"
echo
echo "$(basename "$WHEEL")  --  $(stat -c%s "$WHEEL") bytes, $(echo "$LIST" | wc -l) files"
echo
echo "wheel $VERSION in $OUT"
