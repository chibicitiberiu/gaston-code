#!/usr/bin/env bash
# Redefine the guest from vm/win98.xml.
#
# Two things this exists to stop, both of which have already bitten:
#   * "--" inside an XML comment. libvirt rejects the whole file and the
#     error points at the wrong line, so it reads like a schema problem.
#   * Defining a config that boots the floppy first. That silently restarted
#     Windows Setup over a working installation.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
XML=vm/win98.xml
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"

if grep -n -- '--' "$XML" | grep -v '<!--\|-->' ; then
    echo "^^ '--' inside an XML comment; libvirt will refuse this file." >&2
    exit 1
fi
xmllint --noout "$XML"

if grep -q "boot dev='fd'" "$XML"; then
    echo "REFUSING: $XML boots the floppy first. That reruns Setup over the install." >&2
    exit 1
fi
if grep -q "device='floppy'" "$XML"; then
    echo "REFUSING: $XML still has a floppy device attached." >&2
    exit 1
fi

virsh define "$XML"
virsh domblklist retroclaude-win98 2>/dev/null || true
