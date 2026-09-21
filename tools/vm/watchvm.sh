#!/usr/bin/env bash
# Passive monitor for the Win98 guest: screenshot, read the screen, log change.
#
# Reads QEMU's device model rather than anything inside the guest, so it keeps
# working when the guest is wedged -- which is when it matters most. This is
# the same mechanism the plan relies on for detecting a hung guest or a BSOD.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
# Scratch goes under tmp/, which is gitignored wholesale. It used to land in
# vm/, beside the disk images, which made "what in here matters" unanswerable.
OUT="${OUT:-tmp/watch}"
EVERY="${EVERY:-20}"
N="${N:-900}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"
echo $$ > tmp/watch.pid

have_ocr=0
if command -v tesseract >/dev/null 2>&1; then have_ocr=1; fi

last_dim=""
last_note=""

for i in $(seq 1 "$N"); do
    ts=$(date +%H:%M:%S)
    state=$(virsh domstate "$DOM" 2>/dev/null | head -1)
    if [ "$state" != "running" ]; then
        echo "$ts  state=$state"
        sleep "$EVERY"
        continue
    fi

    f=$(printf "%s/%04d" "$OUT" "$i")
    if virsh screenshot "$DOM" "$f.ppm" >/dev/null 2>&1; then
        magick "$f.ppm" "$f.png" 2>/dev/null && rm -f "$f.ppm"
    else
        sleep "$EVERY"
        continue
    fi

    dim=$(magick identify -format '%wx%h' "$f.png" 2>/dev/null)
    if [ "$dim" != "$last_dim" ]; then
        echo "$ts  video mode $dim"
        last_dim="$dim"
    fi

    if [ "$have_ocr" -eq 1 ]; then
        # Win98 Setup is white on blue; tesseract needs it inverted.
        magick "$f.png" -colorspace gray -negate -resize 180% "$TMP/o.png" 2>/dev/null
        txt=$(tesseract "$TMP/o.png" - 2>/dev/null | tr -d '\r')
        pct=$(printf '%s' "$txt" | grep -oE '[0-9]+%' | head -1)
        key=$(printf '%s' "$txt" | grep -oiE 'Setup is preparing|Setting up hardware|Restarting|Welcome to Windows|Enter Network Password|invalid command|error|Windows Protection' | head -1)
        note="${key:-} ${pct:-}"
        if [ -n "${note// /}" ] && [ "$note" != "$last_note" ]; then
            echo "$ts  $note"
            last_note="$note"
        fi
    fi
    sleep "$EVERY"
done
