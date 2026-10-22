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
- **Open Watcom 2.0**, plain C, ANSI Win32 APIs only.

## Layout

```
docs/       PLAN.md, FEASIBILITY.md, FORKED.md, PROGRESS.md
make/       ow.mk -- shared Open Watcom rules
win/        the guest executables -- GASTON.EXE, GREMOTE.EXE, GUPDATE.EXE
bridge/     Python: Agent SDK, MCP tools, pagination, markdown, compression
gremote/    Python driver for the guest server, and how a build is shipped
tests/      protocol, markdown, encoding, PE import allowlist
tests/guest/  the check binaries: the shipping C run under Wine
research/   apparatus, not product -- probes, their host stubs, the numbers
tools/      qmp.py, vm scripts, the build's own gates
vm/         QEMU run scripts (images gitignored)
```

`win/`, `bridge/`, `gremote/` and `tools/check/` are the product and the
build. `tests/` and `research/` are how it is judged: nothing under either
ships, and nothing under `win/` exists to be measured.

## Status

Phase 0. See `docs/PROGRESS.md`.

## Build

```sh
make              # build the Win98 guest binary
make check        # static PE audit of the guest
make check        # static PE audit: version gate, imports, CMOV, size
make wine-smoke   # run under Wine (code sanity only, NOT a Win98 proof)
```

Requires Open Watcom 2.0 at `/usr/bin/watcom` (override with `WATCOM=`).

## Licence

MIT. Portions of the guest agent derive from retro-agent (MIT) — see `NOTICE`
and `docs/FORKED.md`.
