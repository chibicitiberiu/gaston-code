#!/usr/bin/env bash
# Run the Win98 guest.
#
#   runvm.sh install   first boot: floppy once, then hard disk, CD attached
#   runvm.sh run       normal boot, 32MB to match the real target machine
#
# QMP lives on vm/qmp.sock; drive it with tools/vm/qmp.py.
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
HERE="$REPO"
VM="$HERE/vm"
LIB="${WIN98_LIB:-/mnt/zizo/Library/Software/Operating Systems/Windows/Windows 9x}"
ISO="${WIN98_ISO:-$LIB/Win98SE_bootable.iso}"
MODE="${1:-run}"

# A visible window by default so the install can be watched; QMP still drives
# it either way. VIEW=none keeps it headless for CI.
#
# The remote head is SPICE rather than VNC, to match vm/win98.xml. VNC's
# pointer handling against a PS/2 mouse gives two cursors moving at different
# speeds, with clicks registering only while the outer one is over the window
# -- and usb-tablet, the usual fix, is not an option here because Win98 has no
# HID tablet driver. SPICE does client-side pointer handling and does not have
# the problem. Nothing the guest sees changes either way.
VIEW="${VIEW:-gtk}"
SPICE_PORT="${SPICE_PORT:-5901}"

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
#   pcnet     -- the NIC most consistently reported working on Win98, AND NOT
#                THE ONE vm/win98.qcow2 HAS A DRIVER FOR. The libvirt domain
#                `retroclaude-win98` gives that disk an `ne2k_isa` at
#                iobase=0x200 irq=0x3, hand-configured once, and the installed
#                driver matches THAT. Booting the same disk through this script
#                comes up to "Your network adapter NE2000 Compatible (0000) is
#                not working properly" and has no network at all (measured
#                2026-10-02). Use `virsh -c qemu:///session start
#                retroclaude-win98` for anything that needs the bridge; this
#                script is for a guest that only needs a screen. Both open the
#                same qcow2, so never run both.
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
  -spice port="$SPICE_PORT",addr=127.0.0.1,disable-ticketing=on \
  -display "$VIEW" \
  -name "Gaston Code Win98SE"
