#!/usr/bin/env bash
# Rebuild vm/tools.iso and put it in the guest's CD drive for real.
#
# `virsh change-media --update` with the SAME source path is a no-op: the drive
# keeps the file it already has, so a freshly rebuilt ISO is never seen and the
# guest quietly keeps running the previous binary. That cost an evening of
# chasing a bug that had already been fixed. Eject first, then insert.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"
DOM=retroclaude-win98
ISO="$PWD/vm/tools.iso"

bash tools/mktools-iso.sh
virsh change-media "$DOM" hdc --eject --live >/dev/null 2>&1 || true
sleep 1
virsh change-media "$DOM" hdc "$ISO" --insert --live
sleep 2
echo "host   $(stat -c%s vm/tools.iso) bytes  $(md5sum vm/tools.iso | cut -c1-12)"
echo "exe    $(stat -c%s build/win/retroclaude.exe) bytes"
