# Forked code

Everything here is derived from **retro-agent** (MIT), upstream commit
`82c8d4e68363a70beccd29cdcac78d8feb747c02`, dated 2026-09-16.

Upstream: https://github.com/voidsstr/retro-agent
Licence text reproduced in `/NOTICE`.

This file is both the MIT attribution and the upstream-diff tool: when
retro-agent moves, this is how you work out what changed under you.

## Status

Nothing forked yet. Phase 3 and Phase 6 bring code in.

## Planned

| Upstream file | Lines | Destination | Planned changes |
|---|---:|---|---|
| `agent/src/exec.c` | 361 | `win/exec.c` | Keep the `send_response(SOCKET, ...)` call shape, replace the body to emit our frame grammar. Drop `handle_launch` (pulls in the JSON builder). Fix: unconditional `taskkill` on timeout (absent on 9x), and two `HeapReAlloc` failure paths that leak the original block. |
| `agent/src/input.c` | 490 | `win/input.c` | Near-verbatim. `UICLICK`/`UIDRAG`/`UIKEY`/`WINLIST` are pure syscalls. Keep the E0-extended-scancode table and the `VK_SNAPSHOT`-returns-0 fallback to scancode `0x37`. |
| `agent/src/screen.c` | 467 | — | **Not forked.** Replaced by ~60 lines under "the guest transforms nothing". The only part worth lifting is `screen_palette_for_capture()`: on an 8bpp display a fresh memory DC carries only the 20 static system colours, so `GetDIBits` needs the live hardware palette selected in or it returns colour-index noise. |
| `agent/src/handlers.c` | 514 | `win/rpc.c` | Dispatch table pruned to PING / EXEC / EXECW / DOWNLOAD (~130 lines). |
| `agent/src/main.c` | 1381 | — | **Not forked.** Listener, `select()` multiplex, discovery, service and watchdog scaffolding, all deleted by the single-binary / single-connection design. |
| `agent/src/util.c` | 259 | — | **Not forked.** ~200 lines of it is the JSON builder, and the guest never emits JSON. |
| `agent/shared/frameproto.h` | 37 | `win/frameproto.h` | Reference only; our grammar differs (no status byte, full duplex, `key=value` headers). **`MAX_FRAME_SIZE` must NOT be inherited — upstream is 32MB, which is the entire target machine.** Ours is 64KB. |
| `tests/python/test_agent_win9x_imports.py` | 287 | `tests/python/test_pe_imports.py` | The ~45-line `pe_imports()` PE32 parser is the reusable part. Invert a ban list into an allowlist built from real Win98 DLL exports. |

## Ideas taken without code

- Length-prefixed framing (`[uint32 LE len][payload]`) — the shape, not the bytes.
- `PeekNamedPipe` non-blocking poll for child output, so a hung child cannot stall
  the process. **Note: this prevents hangs, it does not make 16-bit child capture
  work.** See the Phase 0 experiment.
- The PE import-table regression test as a concept: a static import Win9x lacks
  kills the EXE at load, before `main()`, with no log output, and a source grep
  cannot see it.
- Graceful `shutdown(SD_SEND)` before `closesocket`, because an abrupt RST is
  reported to crash Win98's Winsock stack.
