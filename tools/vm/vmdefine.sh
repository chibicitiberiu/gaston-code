#!/usr/bin/env bash
# Redefine the guest from vm/win98.xml.
#
# Two things this exists to stop, both of which have already bitten:
#   * "--" inside an XML comment. libvirt rejects the whole file and the
#     error points at the wrong line, so it reads like a schema problem.
#   * Defining a config that boots the floppy first. That silently restarted
#     Windows Setup over a working installation.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
cd "$REPO"
XML=vm/win98.xml


if grep -n -- '--' "$XML" | grep -v '<!--\|-->' ; then
    echo "^^ '--' inside an XML comment; libvirt will refuse this file." >&2
    exit 1
fi
xmllint --noout "$XML"

if grep -q "boot dev='fd'" "$XML"; then
    echo "REFUSING: $XML boots the floppy first. That reruns Setup over the install." >&2
    exit 1
fi
# THE FLOPPY GUARD IS NOW A WARNING, because the thing it refuses is the
# committed configuration. It was written when a floppy in the XML meant a
# leftover install disk that would boot ahead of the hard disk; vm/xfer.img
# was added afterwards as the guest's way to hand files back, and from that
# day this script could not define vm/win98.xml at all -- it exited 1 on the
# repo's own file. Booting from a floppy is the real hazard and `boot dev`
# above still refuses it outright.
#
# What it costs is real and is said out loud: a raw floppy means QEMU's
# internal snapshots (qmp.py save/load) do not work on this domain. The
# rollback is tools/vm/vmsnap.sh.
if grep -q "device='floppy'" "$XML"; then
    echo "vmdefine: note: $XML attaches a floppy (vm/xfer.img). That is the
            guest's return channel and is expected. It does mean QEMU's
            internal snapshots are unavailable -- use tools/vm/vmsnap.sh."
fi

virsh define "$XML"
virsh domblklist "$DOM" 2>/dev/null || true
