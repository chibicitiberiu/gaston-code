#!/usr/bin/env bash
# Screenshot the guest as PNG. Compare raw PPM, never PNG md5 -- ImageMagick
# stamps metadata, so two captures of an identical screen hash differently and
# an "is it still animating?" test silently inverts.
#
# Two SELinux facts decide where the intermediate PPM goes, and both cost an
# AVC denial to learn:
#
#   * QEMU runs confined as svirt_tcg_t. It cannot write into this repo, which
#     is labelled user_home_t -- so the old default of vm/shot.ppm failed with
#     "Permission denied" and raised an alert. Relabelling the repo would be
#     the wrong fix: it is a git working tree, not an image store.
#   * The file must NOT be pre-created. QEMU creating a new file in a
#     user_tmp_t directory gets a type transition to svirt_tmp_t and is
#     allowed; a file mktemp already made is user_tmp_t, and writing to that
#     is denied. So this uses `mktemp -u` for a name only.
set -euo pipefail
OUT="${1:-${TMPDIR:-/tmp}/vmshot.png}"
PPM="$(mktemp -u "${TMPDIR:-/tmp}/vmshot-XXXXXX.ppm")"
trap 'rm -f "$PPM"' EXIT
python3 "$(dirname "${BASH_SOURCE[0]}")/qmp.py" shot "$PPM"
mkdir -p "$(dirname "$OUT")"
magick "$PPM" "$OUT"
echo "$OUT"
