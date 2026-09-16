#!/usr/bin/env bash
# Screenshot the guest as PNG. Compare raw PPM, never PNG md5 -- ImageMagick
# stamps metadata, so two captures of an identical screen hash differently and
# an "is it still animating?" test silently inverts.
set -euo pipefail
OUT="${1:-vm/shot.png}"
PPM="${OUT%.png}.ppm"
python3 "$(dirname "${BASH_SOURCE[0]}")/qmp.py" shot "$PPM"
magick "$PPM" "$OUT"
echo "$OUT"
