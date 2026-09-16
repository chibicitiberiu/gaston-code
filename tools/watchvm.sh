#!/usr/bin/env bash
# Poll the guest's screen and log what it is doing.
#
# Screenshots come from QEMU's device model, not from anything inside the
# guest, so this keeps working when the guest is wedged -- which is the point.
set -uo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///session
DOM="${DOM:-retroclaude-win98}"
OUT="${OUT:-vm/watch}"
EVERY="${EVERY:-20}"
N="${N:-90}"
mkdir -p "$OUT"
for i in $(seq 1 "$N"); do
    ts=$(date +%H:%M:%S)
    if ! virsh domstate "$DOM" 2>/dev/null | grep -q running; then
        echo "$ts  domain not running: $(virsh domstate "$DOM" 2>&1 | head -1)"
        break
    fi
    f=$(printf "%s/%03d" "$OUT" "$i")
    if virsh screenshot "$DOM" "$f.ppm" >/dev/null 2>&1; then
        magick "$f.ppm" "$f.png" 2>/dev/null && rm -f "$f.ppm"
        # Cheap change detector so the log shows when something happened.
        sum=$(md5sum "$f.png" 2>/dev/null | cut -c1-8)
        echo "$ts  shot $i  $sum"
    else
        echo "$ts  screenshot failed"
    fi
    sleep "$EVERY"
done
