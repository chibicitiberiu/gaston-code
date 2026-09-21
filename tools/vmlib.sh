# Shared by the shell scripts in tools/. Source it, do not run it:
#
#     . "$(dirname "${BASH_SOURCE[0]}")/../vmlib.sh"
#
# Same job as tools/toolkit.py and for the same reason. Six scripts computed
# the repo root with `cd "$(dirname "${BASH_SOURCE[0]}")/.."`, counting one
# level, and five of them also carried their own copy of the domain name --
# three with no environment override at all, so pointing the tools at a
# second VM meant editing three files.
#
# REPO is found by walking up for the markers rather than by counting, so a
# script can move between tools/vm/ and tools/gen/ without silently resolving
# to tools/ and writing its output there.

rc_find_repo() {
  # BASH_SOURCE[0] inside a function is the file the function was DEFINED in,
  # so this walks up from vmlib.sh itself -- independent of which script
  # sourced it and of the caller's working directory.
  local d
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  while [ "$d" != "/" ]; do
    if [ -f "$d/CLAUDE.md" ] && [ -d "$d/bridge" ] && [ -d "$d/win" ]; then
      printf '%s\n' "$d"; return 0
    fi
    d="$(dirname "$d")"
  done
  echo "vmlib: cannot find the repo root above ${BASH_SOURCE[0]}" >&2
  return 1
}

REPO="$(rc_find_repo)" || exit 1

#: The libvirt domain these scripts drive, and the connection they drive it
#: over. `retrobridge` in qemu:///system belongs to an unrelated project and
#: nothing here may touch it -- which is a reason for the domain to be one
#: named value rather than a string retyped in five places.
DOM="${RC_DOMAIN:-retroclaude-win98}"
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"
URI="$LIBVIRT_DEFAULT_URI"
