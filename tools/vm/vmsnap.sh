#!/usr/bin/env bash
# vmsnap.sh -- a known-good copy of the guest disk, and the way back to it.
#
# BACKLOG.md 15 asks for "a known-good rollback in the loop" before a run that
# puts our code inside other programs. Until now that was eight hand-made
# `cp` files in vm/ with no script behind them, which is a habit rather than a
# mechanism: the one run nobody copied first is the one that needs it.
#
# A FILE COPY, NOT A LIBVIRT SNAPSHOT, and that is forced rather than chosen.
# `virsh snapshot-create-as` wants every disk in a format that can hold one,
# and vm/xfer.img is attached raw as the floppy -- which is also the guest's
# only way to hand a file back when the network is the thing being tested. So
# `qmp.py save`/`load` (savevm/loadvm) fail for the same reason, which that
# file now says beside them. Detaching the floppy to get snapshots back costs
# a full stop and start of the domain (libvirt refuses floppy hotplug) and
# costs the return channel; copying 600MB costs about three seconds.
#
# THE DOMAIN MUST BE OFF. Copying a qcow2 under a running guest captures a
# torn image: the bytes a crash-consistent copy would have, from a machine
# that was not crashing, and the first thing that reads it is a 1998 scandisk.
#
#   tools/vm/vmsnap.sh save [label]     -> vm/win98.qcow2.known-good-<ts>[-label]
#   tools/vm/vmsnap.sh list
#   tools/vm/vmsnap.sh restore <file>   (the live image is copied aside first)
#
# `restore` keeps the image it is about to overwrite, under
# `.before-restore-<ts>`. A rollback that destroys the evidence of what went
# wrong is why the run was being rolled back in the first place.
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"

DISK="$REPO/vm/win98.qcow2"
PREFIX="$DISK.known-good-"

die() { echo "vmsnap: $*" >&2; exit 1; }

require_off() {
  local state
  state="$(virsh domstate "$DOM" 2>/dev/null || echo unknown)"
  case "$state" in
    "shut off"|"unknown") : ;;
    *) die "$DOM is '$state'. Stop it first (virsh destroy $DOM) -- a qcow2
        copied under a running guest is a torn image." ;;
  esac
}

human() { du -h "$1" 2>/dev/null | cut -f1; }

case "${1:-}" in
  save)
    [ -f "$DISK" ] || die "no $DISK"
    require_off
    label="${2:-}"
    out="$PREFIX$(date +%Y%m%d-%H%M%S)${label:+-$label}"
    cp -f --reflink=auto "$DISK" "$out"
    # Verified by reading it back, not by cp's exit code: the rule this
    # script exists for says "verify recovery by actually restoring or
    # counting", and a header read is the cheap half of that.
    qemu-img info "$out" >/dev/null || die "wrote $out and cannot read it back"
    echo "vmsnap: saved $(human "$out") -> $out"
    ;;
  list)
    ls -1t "$PREFIX"* 2>/dev/null || echo "vmsnap: nothing saved yet"
    ;;
  restore)
    src="${2:-}"
    [ -n "$src" ] || die "restore needs a file (vmsnap.sh list)"
    [ -f "$src" ] || die "no such file: $src"
    require_off
    aside="$DISK.before-restore-$(date +%Y%m%d-%H%M%S)"
    cp -f --reflink=auto "$DISK" "$aside"
    cp -f --reflink=auto "$src" "$DISK"
    qemu-img info "$DISK" >/dev/null || die "restored and cannot read it back"
    echo "vmsnap: restored $src"
    echo "vmsnap: what was there is at $aside"
    ;;
  *)
    sed -n '1,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
