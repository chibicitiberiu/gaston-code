#!/usr/bin/env bash
# Sit with the unattended Win98 install.
#
# Two jobs:
#
#  1. Eject the boot floppy before Setup's first reboot. Boot order is fd,hd;
#     with the floppy still in, that reboot lands back on it and starts over.
#     Ejecting makes the BIOS fall through to the hard disk, which is bootable
#     by then because the floppy ran `sys c:`.
#
#     Detected WITHOUT OCR: the Setup GUI runs at 640x480 and the post-reboot
#     BIOS/DOS screen is 720x400, so the screenshot's DIMENSIONS change.
#     Dependency-free and unambiguous, so it still works if OCR is unavailable.
#
#  2. Read the screen so the log says what is happening. Win98 Setup is white
#     text on blue, which tesseract cannot read without inverting first.
#
# Both read QEMU's device model rather than anything inside the guest, so they
# keep working when the guest is wedged -- which is exactly when it matters.
set -uo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///session
DOM=retroclaude-win98
OUT=vm/sitter
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"
echo $$ > vm/sitter.pid

have_ocr=0
command -v tesseract >/dev/null 2>&1 && have_ocr=1

ejected=0
last_dim=""
last_note=""

ocr_sidebar() {
    [ "$have_ocr" -eq 1 ] || return 0
    magick "$1" -crop 132x480+0+0 +repage -colorspace gray -negate \
        -resize 250% "$TMP/s.png" 2>/dev/null || return 0
    tesseract "$TMP/s.png" - 2>/dev/null | tr -d '\r'
}

for i in $(seq 1 500); do
    ts=$(date +%H:%M:%S)
    state=$(virsh domstate "$DOM" 2>/dev/null | head -1)
    if [ "$state" != "running" ]; then
        echo "$ts  state=$state"
        sleep 10; continue
    fi

    f="$OUT/$(printf %03d "$i")"
    virsh screenshot "$DOM" "$f.ppm" >/dev/null 2>&1 || { sleep 10; continue; }
    magick "$f.ppm" "$f.png" 2>/dev/null && rm -f "$f.ppm"
    dim=$(magick identify -format '%wx%h' "$f.png" 2>/dev/null)

    if [ "$ejected" -eq 0 ] && [ -n "$last_dim" ] && [ "$dim" != "$last_dim" ] \
       && [ "$dim" != "640x480" ]; then
        echo "$ts  video mode $last_dim -> $dim : reboot -- ejecting floppy"
        virsh change-media "$DOM" fda --eject --live 2>&1 | tail -1
        ejected=1
    fi
    [ "$dim" != "$last_dim" ] && echo "$ts  video mode $dim"
    last_dim="$dim"

    txt=$(ocr_sidebar "$f.png")
    pct=$(echo "$txt" | grep -oE '[0-9]+%' | head -1)
    mins=$(echo "$txt" | grep -oE '[0-9]+ minutes' | head -1)
    phase=$(echo "$txt" | grep -oiE 'Copying|Setting up hardware|Restarting|Collecting information|Preparing to run' | tail -1)
    note="${phase:-?} ${pct:-} ${mins:-}"
    if [ "$note" != "$last_note" ]; then
        echo "$ts  $note"
        last_note="$note"
    fi
    sleep 15
done
echo "sitter finished"
