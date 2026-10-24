# miniz 3.0.2 (vendored)

Single-file deflate/inflate. MIT. Upstream: https://github.com/richgel999/miniz

Vendored rather than submoduled because it is two files and the guest build
must be reproducible without network access.

## Verified under Open Watcom

`wcc386 -bt=nt -bm -otexan -5` compiles `miniz.c` with **zero errors and zero
warnings**, and a round-trip test runs correctly as a Win32 PE (subsystem 4.00,
no CMOV, imports only KERNEL32/USER32). Adds roughly 95KB to a linked binary.

## Settings this project uses

See `docs/ARCHITECTURE.md`. Summary: one persistent deflate stream per direction with
`Z_SYNC_FLUSH` per frame, 32KB window, level 6 bridge->guest and level 1
guest->bridge. Do **not** deflate each frame independently -- measured 1.27x
worse.
