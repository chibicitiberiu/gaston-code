#!/usr/bin/env bash
# Run the Win98 guest.
#
#   runvm.sh install   first boot: floppy once, then hard disk, CD attached
#   runvm.sh run       normal boot, 32MB to match the real target machine
#
# QMP lives on vm/qmp.sock; drive it with tools/qmp.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM="$HERE/vm"
LIB="${WIN98_LIB:-/mnt/zizo/Library/Software/Operating Systems/Windows/Windows 9x}"
ISO="${WIN98_ISO:-$LIB/Win98SE_bootable.iso}"
MODE="${1:-run}"

# A visible window by default so the install can be watched; QMP still drives
# it either way. DISPLAY=none keeps it headless for CI, and VNC stays on :1
# regardless so it can also be attached to remotely.
VIEW="${VIEW:-gtk}"

# The real machine is a Pentium MMX 200 with 32MB. Install with more RAM
# because Setup is painfully slow otherwise; Win98 does not bake the size in,
# so `run` drops back to 32MB to match the target.
case "$MODE" in
  install) MEM=128; BOOT="once=a,order=c"; FLOPPY=(-fda "$VM/boot98.img") ;;
  run)     MEM="${MEM:-32}";  BOOT="order=c"; FLOPPY=() ;;
  *) echo "usage: runvm.sh [install|run]" >&2; exit 2 ;;
esac

rm -f "$VM/qmp.sock"

# Hardware choices, all from the compatibility notes in docs/PLAN.md:
#   i440fx    -- not q35; Win98 predates it
#   pentium2  -- matches the target class and avoids the fast-CPU loop bug
#   pcnet     -- the NIC most consistently reported working on Win98
#   cirrus    -- has an in-box Win98 driver
#   usb-tablet is NOT used: Win98 has no HID tablet driver, so absolute
#   positioning would silently do nothing. PS/2 mouse it is.
exec qemu-system-i386 \
  -machine pc-i440fx-9.2,acpi=on \
  -cpu pentium2 \
  -m "$MEM" \
  -drive file="$VM/win98.qcow2",format=qcow2,if=ide,index=0,media=disk \
  -drive file="$ISO",format=raw,if=ide,index=2,media=cdrom,readonly=on \
  "${FLOPPY[@]}" \
  -boot "$BOOT" \
  -vga cirrus \
  -netdev user,id=n0,hostfwd=tcp::17890-:7890 \
  -device pcnet,netdev=n0 \
  -rtc base=localtime \
  -qmp unix:"$VM/qmp.sock",server=on,wait=off \
  -vnc :1 \
  -display "$VIEW" \
  -name "RetroClaude Win98SE"
