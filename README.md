# Gaston Code

A native Windows 98 GUI client for Claude Code.

The Win98 machine is both the terminal you sit at and the workspace Claude
operates on. A bridge process on a modern Linux box runs Claude Code via the
Agent SDK and speaks a small framed protocol over the LAN to a single
executable on the guest.

Nobody has built this. The closest prior art each solves half of it:
[claudewin9x](https://github.com/ryandeering/claudewin9x) proxies the real CLI
but its client is a console REPL; [retro-agent](https://github.com/voidsstr/retro-agent)
has a complete guest tool surface and a hardened protocol but is headless and
drives the Agent SDK rather than Claude Code.

## Why "Gaston Code"?

The name follows Claude Code. Claude is a French name, and Gaston is another
one: a companion name rather than a borrowed one.

It is also a nod to Gaston Lagaffe, André Franquin's office junior from
*Spirou* (1957 onwards). He works in a busy office, naps when he should be
filing, and builds contraptions on the office machines that nobody asked for.
Most of them go wrong in some memorable way. Handing an AI agent the keys to a
beige 1998 PC felt like the same spirit.

The application icon is a monitor with a mop-haired figure in a green
turtleneck on the screen. It is our own drawing, a wink at the character
rather than a copy of Franquin's art.

## Constraints that shape everything

- **32MB RAM**, ~8–12MB free after boot. Win98 swaps on a period IDE disk, so
  working-set size matters more than CPU.
- **Pentium MMX 200MHz.** Compression is a tradeoff, not a freebie.
- **10Mbps LAN (~750 KB/s), sometimes dialup (~5 KB/s).** Bandwidth is a
  first-class design input.
- **Open Watcom 2.0**, C++98 with its STL, ANSI Win32 APIs only. Nothing
  NT-only, ever: one NT-only static import kills the image at load,
  before `WinMain`, with no log written.

## Layout

```
docs/       PLAN.md the design, PROGRESS.md what was built, BACKLOG.md
            what was deferred, ROADMAP.md the order of the rest
make/       ow.mk -- shared Open Watcom rules
win/        the guest executables -- GASTON.EXE, GREMOTE.EXE, GUPDATE.EXE
bridge/     Python: Agent SDK, MCP tools, pagination, markdown, compression
gremote/    Python driver for the guest server, and how a build is shipped
tests/      protocol, markdown, encoding, PE import allowlist
            guest/ is the check binaries: the shipping C, run under Wine
research/   apparatus, not product -- probes, their host stubs, the numbers
tools/      check/ the build's own gates, vm/ QEMU and QMP scripts,
            gen/ the icon, the tools ISO and the install kit
vm/         QEMU run scripts (images gitignored)
```

`win/`, `bridge/`, `gremote/` and `tools/check/` are the product and the
build. `tests/` and `research/` are how it is judged, and nothing under
either ships. The split is deliberate: `win/checks/` used to hold both the
Wine test binaries and the probes that measure a real 1998 machine, under
one directory name that said neither.

## Status

Phases 0 through 5 are built and demonstrated on real hardware; phase 6 is
part-built. As of 2026-10-23 it runs on a physical Pentium 200 MMX under
Windows 98 SE, installed over the network, driving a tool-calling turn end
to end. What is left is a clean install by somebody who is not the author,
and publishing. See `docs/PROGRESS.md` and `docs/ROADMAP.md`.

## Build

```sh
make              # build the Win98 guest binary
make check        # static PE audit of the guest: imports, CMOV, frames, BSS
make test-fast    # the Python suite except the Wine runs -- the inner loop
make wine-smoke   # run under Wine (code sanity only, NOT a Win98 proof)
```

Requires Open Watcom 2.0 at `/usr/bin/watcom` (override with `WATCOM=`).

## Licence

MIT. Portions of the guest agent derive from retro-agent (MIT) — see `NOTICE`
and `docs/FORKED.md`.
