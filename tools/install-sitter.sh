#!/usr/bin/env bash
# Sit with the unattended Win98 install and eject the boot floppy.
#
# WHY THIS IS FIDDLY, recorded because it cost two restarts:
#
#   The floppy must boot once (to run `sys c:` and launch Setup) and must NOT
#   be there for Setup's reboot -- otherwise the reboot lands back on it, the
#   floppy's AUTOEXEC.BAT runs SETUP.EXE again, and the install restarts from
#   0% forever.
#
#   Two ways to get this wrong, both of which I did:
#     * ejecting at the moment the reboot is detected: too late, the BIOS has
#       already selected the floppy, so DOS then cannot find COMMAND.COM and
#       drops to an "A>" prompt;
#     * ejecting with --live but not --config: the running domain loses the
#       media, but any restart re-inserts it from the XML.
#
#   So: eject EARLY (as soon as Setup's GUI is up, by which point MSBATCH.INF
#   has been read) and eject from BOTH the live domain and the persistent
#   config.
set -uo pipefail
export LIBVIRT_DEFAULT_URI=qemu:///session
DOM=retroclaude-win98
# Scratch under tmp/, not beside the disk images. See watchvm.sh.
OUT=tmp/sitter
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"; echo $$ > tmp/sitter.pid

have_ocr=0; command -v tesseract >/dev/null 2>&1 && have_ocr=1
ejected=0; gui_seen=0; last_dim=""; last_note=""

for i in $(seq 1 600); do
    ts=$(date +%H:%M:%S)
    state=$(virsh domstate "$DOM" 2>/dev/null | head -1)
    if [ "$state" != "running" ]; then echo "$ts  state=$state"; sleep 10; continue; fi

    f="$OUT/$(printf %03d "$i")"
    virsh screenshot "$DOM" "$f.ppm" >/dev/null 2>&1 || { sleep 8; continue; }
    magick "$f.ppm" "$f.png" 2>/dev/null && rm -f "$f.ppm"
    dim=$(magick identify -format '%wx%h' "$f.png" 2>/dev/null)
    [ "$dim" != "$last_dim" ] && echo "$ts  video mode $dim"
    last_dim="$dim"

    # Eject once the file copy is actually underway (a percentage on screen).
    # Ejecting on "GUI is up" alone was too early: Setup reads its batch file
    # lazily, so pulling the floppy out mid-read produced
    # "SU995035: An invalid command option was specified". The answer file now
    # lives on C:, so by the time copying starts nothing needs A: at all.
    if [ "$dim" = "640x480" ]; then gui_seen=$((gui_seen + 1)); else gui_seen=0; fi
        echo "$ts  Setup GUI up -- ejecting floppy (live AND config)"
        virsh change-media "$DOM" fda --eject --live   2>&1 | tail -1
        virsh change-media "$DOM" fda --eject --config 2>&1 | tail -1
        left=$(virsh dumpxml "$DOM" | grep -c "boot98.img" || true)
        echo "$ts  floppy references left in live XML: $left"
        ejected=1
    fi

    pct=""
    if [ "$have_ocr" -eq 1 ]; then
        magick "$f.png" -crop 132x480+0+0 +repage -colorspace gray -negate \
            -resize 250% "$TMP/s.png" 2>/dev/null
        txt=$(tesseract "$TMP/s.png" - 2>/dev/null | tr -d '\r')
        pct=$(echo "$txt" | grep -oE '[0-9]+%' | head -1)
        mins=$(echo "$txt" | grep -oE '[0-9]+ minutes' | head -1)
        note="${pct:-} ${mins:-}"
        if [ -n "${note// /}" ] && [ "$note" != "$last_note" ]; then
            echo "$ts  copy ${note}"
            last_note="$note"
        fi
    fi

    if [ "$ejected" -eq 0 ] && [ -n "$pct" ]; then
        echo "$ts  copying ($pct) -- ejecting floppy (live AND config)"
        virsh change-media "$DOM" fda --eject --live   2>&1 | tail -1
        virsh change-media "$DOM" fda --eject --config 2>&1 | tail -1
        ejected=1
    fi
    sleep 15
done
