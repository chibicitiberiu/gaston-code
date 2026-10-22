# libfs plan (REMOTE.md step 5a)

## Context

`libfs` is the last of the five libraries. Four exist (`libcore`, `libwire`,
`libexec`, `libautomation`); this one blocks `gremote`, because the fs lane
(`LS STAT GET PUT RM MKDIR MV DRIVES`) has nothing to call.

The code exists, scattered. `win/sync/sync.cpp` holds a directory walk, a stat,
and the temp-write/verify/rename swap that `BACKLOG.md` 1213 says not to
reinvent. `win/app/export.cpp` holds a streaming sink that `BACKLOG.md` 6 calls
the third implementation of the same shape, and the cleanest to extract from.
`win/app/workcopy.cpp` holds the free-space call. Three joins exist with two
semantics.

A second problem the same library fixes: the agent cannot touch a file outside
the mirrored folder. `type` mangles binary, caps at 32KB and returns console
codepage bytes; nothing writes a binary file out there at all.

**Step 5a is this plan: the library and the client.** `ship()` and self-update
need `GREMOTE.EXE` (step 4). `CANCEL` needs an id-queue class in `win/rpc/`.

## Scope decisions

- The classifier and its dual CRC **stay in `win/sync/`**. CLAUDE.md scopes the
  transforms-nothing exception to *the sync layer's* classification; it mirrors
  `bridge/files/paths.py` and honours `.gitattributes`, which libfs has never
  heard of. gremote's GET is byte-exact. libfs gives it a one-pass read hook so
  nothing reads a file twice.
- `rc_mkpath` stays in libcore: seven call sites, five of them not fs work, and
  it is a C translation unit where libfs is C++-only.
- Guest-side writes through the new agent tools are **unconfined**, like `exec`.

## API

### `fs/path.h`

`isContainedRelative(rel)`; `struct WorkNames { tmpSuffix, oldSuffix, reserve,
aside }`; `isWorkingName`; `fitsWithReserve`.

`WorkNames::aside` is a policy flag, not a constant. Sync wants
move-aside-then-rename so no instant has no file. Export wants delete + rename
with the temp left findable on failure. `FSPUT` outside the mirror wants no
aside at all, because nothing will ever run a recovery pass in that directory.

### `fs/file.h`

`struct FileInfo { type, size, mtime, attrs }`; `statPath`; `removeFile`;
`movePath`; `makeDirectory`.

### `fs/dir.h`

`class DirSink { onEntry(rel, FileInfo) }`; `class WalkFilter { skip(name) }`;
`listDirectory`; `struct WalkLimits`; `enum WalkResult { OK, NO_ROOT, OVERFLOW
}`; `walkTree`.

`OVERFLOW` stays a distinct result and never a bool: a truncated manifest is
indistinguishable from a mass deletion.

### `fs/reader.h`

`class ByteSink { onBytes }`; `class FileReader { open, size, readAll(ByteSink&,
chunk) }`. This is the hook the sync layer's `ContentScanner` plugs into.

### `fs/writer.h`

`class CommitVerifier { verify(tempPath) }`; `enum CommitStatus`; `class
FileWriter { begin, write, commit(CommitVerifier*), abort, tempPath }`;
`recoverInterrupted`.

The verifier is injected and nullable. That is how libfs keeps "PUT with CRC"
while depending on libcore alone: the CRC is miniz, so the client injects a
class-aware dual-CRC verifier, gremote a raw one, and export none.

### `fs/xfer.h`

`class ChunkSink { begin, write(off, p, n) -> {OK, OUT_OF_ORDER, FAILED},
commit, abort(why), received, active }`; `class ChunkConsumer { onChunk }`;
`class ChunkSource { open, total, pump(consumer, tee, chunk, maxBytes) ->
{DONE, TRUNCATED, CONSUMER_STOPPED, READ_FAILED} }`.

`maxBytes` is honoured **on the guest**. A bridge-side cap would still have the
guest stream a swap file over a socket for as long as it takes.

### `fs/drives.h`, `fs/freespace.h`

`struct DriveInfo { letter, type, label, serial, totalBytes, freeBytes }`;
`class DriveSink`; `enumerateDrives`; `freeBytes64`; `freeBytesSaturating`.

Two translation units on purpose: the client links only `freespace.cpp`.
`freeBytes64` returns 64 bits because `workcopy.cpp`'s current version saturates
at `0xFFFFFFFF` and `machine.describe()` prints that figure to the agent on
every request. `freeBytesSaturating` stays for the one caller whose budget
genuinely is a DWORD.

## What moves, and what stays

Moves out of `sync/sync.cpp` (~480 of 799 lines): the walk, the attribute stat,
the chunked read, `begin_write`/`commit`/`recover`, `contained`,
`is_working_name`. `rc_sync_join` is deleted in favour of `Path::join`.

Stays in `win/sync/`: the classifier and dual CRC (`sync/scanner.cpp`), the
manifest row and `statForManifest` (`sync/manifest.cpp`), the SYNCPUT body
parser (`sync/putpath.cpp`, its own TU so `synccheck` does not link the net
layer for one path parser), and the wire adapter (`syncrpc.cpp`).

`RC_CLS_*`, `RC_CRC_FOR`, `RC_SYNC_MAX_*`, the suffixes and
`RC_SYNC_WORK_SUFFIX` stay too: they are declared to the bridge in HELLO and
asserted against `net/caps.h`. libfs takes them as `WalkLimits`/`WorkNames`
parameters.

## The fs lane is a lane, not a table row

`frameproto.h` says a new tool is a row in `RC_LANE_TABLE`, not a branch. That
is true for the lanes that already have a dispatcher. It is **not** true here,
and three things in `win/rpc/rpc.cpp` say so:

- `rpc_thread_body` calls `rc_syncrpc_handle` unconditionally for everything off
  the sync queue, and that function ends in `else rc_log("unhandled frame")`.
- `id_matches_family` is `is_sync(f) ? (id[0]=='s') : (id[0]=='x')` and drops a
  mismatch.
- `reject_sync_busy` defaults to `SYNCPUTACK` whatever arrived.

And `frameproto.h` defines STREAM as "milliseconds of file I/O ... must never
wait behind a JOB". A 30MB `FSGET` is a job. Putting it on STREAM would hold the
single sync worker while the bridge's 300s budget runs down, and fill `g_syncq`
(24 deep), which makes the reader thread spin in `while (syncq_full()) Sleep(10)`
-- stalling every frame, including the UI's.

So FS gets `RC_LANE_FS`, its own queue and worker with a small explicit stack
reserve, `id_matches_family` widened, a `reject_fs_busy` with the right ack per
op, and `FSDAT` added to `rc_frame_compressible`.

## Order of work (small commits, each verified)

Per commit: `make -C win all`, `make -C win synccheck workcheck rpccheck
netcheck cppcheck` (link only), `make -C win check` (record committed-at-load),
`make test-fast`.

The link check is not optional. `sync/sync.cpp` is in **five** object lists
(`OBJS`, `SYNCCHECK_OBJS`, `WORKCHECK_OBJS`, `NETCHECK_OBJS`, `RPCCHECK_OBJS`),
`make all` builds only `gaston.exe`, `make check` runs three gates, and
`test-fast` deselects the guest marker -- so a broken check binary would hide
from commit 1 to commit 5.

1. Docs: this file, REMOTE's fs op table, the classifier line.
2. `fs/path`, the library and its Makefile wiring, `sync/compat.cpp`, all five
   object lists.
3. `fs/reader`; `ContentScanner` into `sync/scanner.cpp` implementing
   `ByteSink`, C facade kept for `rpccheck.c`; `statForManifest`.
4. `fs/file`, `fs/dir`. Reader lands first so the walk shim does not have to
   stat every entry twice on a 2,000-file tree.
5. `fs/writer` with `TempFile`, `ReadOnlyGuard` and `WorkNames::aside`.
6. `fscheck` -- before the adapter rewrites, because it is the gate they are
   measured against.
7. `fs/xfer`.
8. `export.cpp` onto the sink.
9. `do_put`/`do_putend` onto `ChunkSink`; `out_of_order`.
10. `do_get` onto `ChunkSource`.
11. rpccheck ATTGET cases, then `do_attget`.
12. `do_scan`, `do_mkdir`.
13. `fs/drives`; `workcopy`'s `free_bytes` and `dosjoin`; `net.cpp`'s
    `disk_free_kb`.
14. The shims go; `synccheck.c` becomes C++; `sync/sync.cpp` is deleted.
15. `RC_LANE_FS` and `fsrpc.cpp` answering `FSGET`.
16. `FSPUT`, `FSDRIVES`.
17. The bridge: frames, dispatch rows, `guest_fs.py`.
18. The agent: `get_file`, `put_file`, `drives`.

## Traps

- **An abandoned `FSPUT` outside the mirror is unrecoverable.**
  `rc_sync_recover` runs once, on the sync root. Hence `aside=false` for FS.
- **`drives` must not pop a modal.** `GetVolumeInformationA` on an empty `A:`
  trips the 9x critical-error handler -- a dialog on the user's screen from a
  call approved as read-only, blocking the worker until someone dismisses it.
  `SetErrorMode(SEM_FAILCRITICALERRORS)` around the enumeration.
- **A fetched binary must not land in the mirror**, or the next scan pushes it
  back to the guest. Conflict staging already sits one level above `workdir`
  for this reason.
- **The status vocabulary test cuts both ways.** It computes `missing` and
  `extra` against `syncrpc.cpp` alone, so the fs handlers may only use tokens
  that file already produces, and `out_of_order` must reach the wire through
  `put_fail` there.
- Export's sink is stdio with a checked `fclose` (a full floppy fails inside
  it), suffix `.$$$`, no aside, no CRC. Three differences from sync's, all
  carried by `WorkNames` and a nullable verifier.
- `ContentScanner` stays heap-only with its `RC_SYNC_CHUNK + 1` buffer. That
  `+ 1` fixed a live heap overrun and looks exactly like something tidied away
  during a move.

## Verification

Wine settles the grammar. The guest settles the rest:

1. A sync pass both ways, text and binary.
2. An edit-conflict push, then no `.gaston-tmp` anywhere (mem-audit F1).
3. An interrupted PUT, recovered at startup.
4. A read-only target: refused, attribute still set.
5. A path at 251-259 characters: refused, nothing written or deleted.
6. Export, and an export abandoned mid-transfer.
7. `disk_free_kb` on a volume larger than 2GB.
8. An `FSPUT` killed mid-transfer outside the mirror leaves nothing behind.
9. `drives` with an empty floppy in A: does not pop a dialog.
10. A real agent reading a file outside the mirror, writing one, and listing
    the drives -- and the fetched binary not syncing back.

## Progress

Baseline before any change (2026-09-27): `retroclaude.exe` 435,712 bytes,
**448,441 committed at load** against a 470,000 budget.

### Done (2026-09-27)

Eighteen commits, `dcc448c`..`07e6a96`. The library, the client onto it,
`win/sync/sync.cpp` deleted, the fs lane in the client, and the agent's
`get_file`/`put_file`/`drives`.

Committed at load ended at **462,617** (+14,176), with 7,383 of headroom.

Verified on the real Win98 guest, not only under Wine --
`research/measurements/libfs-vm-20260927.txt`:

- `fscheck.exe` 42/42, including the drive map with no critical-error dialog
  on a removable.
- A push and a pull through the rewritten `ChunkSink`/`ChunkSource`, then a
  guest-side edit, a conflict and a resolve -- after which `DIR /S` over the
  sync root found **one file and no working files at all**. That is
  `docs/mem-audit` F1, which used to leak a `.gaston-tmp` on every edit
  conflict.
- All three agent tools end to end through the shipping client, with
  `put_file`'s result checked on the guest rather than taken from the
  agent's report, and no `.gaston-put` left behind.

Found by running it: a mounted CD-ROM was reported to the agent as an empty
drive, because the table inferred no-media from zero bytes free. Fixed in
`07e6a96` and re-verified on the guest.

Two `rpccheck` cases fail on the guest and are not ours: they drive
`cmd.exe /c for /l ...`, which Windows 98 does not have.

### Still open

- **5b**: `ship()` and self-update, which need `GREMOTE.EXE` from step 4.
- `CANCEL` (BACKLOG 6 item 2), which needs the rpc id-queue class.
- `_send_html`'s END frame (BACKLOG 6 item 3).
- The screenshot strips as a `FileWriter` caller -- deferred, because
  `app/screenshot.cpp` is slated for retirement.
- A manifest pass timed on real hardware (BACKLOG 4). The VM is an emulator
  and its timings have misled this project before.
- `disk_free_kb` above 2GB: this guest has 1.57GB free, so the clamp this
  step removed cannot be demonstrated on it.
