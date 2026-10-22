# 05 — The sync layer

Audit date 2026-09-21. Scope: `bridge/files/{sync,syncrun,synctransport,scanner,
manifest,paths,attrs,ignores,reporter,caps}.py`, `bridge/synccoord.py`, and
`win/sync/{sync.h,sync.cpp,syncrpc.cpp}`, read against `docs/PLAN.md` R6 and
`docs/BACKLOG.md` §4, §7 and §11.

Method: both sides read in full. The one finding that turns on behaviour rather
than structure (05.1) was **executed** -- the guest's own `rc_sync_contained` and
`do_put` parser, compiled verbatim and run against realistic file bodies -- rather
than argued from the source.

**This is the strongest part of the codebase.** `diff()` is an exhaustive state
table over `{absent, file, dir, unreadable}` × the baseline states, evaluated in
a stated order, with every row carrying the incident that produced it. The two
invariants at the top of `sync.py` (baseline advances last, from the receiver's
ack; divergence decides direction, never mtime) are the right two, and the code
actually holds to them. Findings below are specific defects, not a verdict on the
design.

---

## 05.1 — HIGH — the guest validates the push path concatenated with the file's
own contents, so any file containing `//` is permanently refused

`win/sync/syncrpc.cpp:285`:

```c
if (!rc_sync_contained(rel)) { put_fail(net, id, "bad_path"); return; }
rc_strf(g_put.id,  sizeof(g_put.id),  "%s", id);
rc_strf(g_put.rel, sizeof(g_put.rel), "%.*s", (int)rn, rel);
```

`rel` points into the frame body at `body + plen + 1`. The body is built by
`bridge/files/synctransport.py:142`:

```python
prefix = str(len(rel_wire)).encode() + b" " + rel_wire
... Frame("SYNCPUT", {...}, prefix + piece)
```

**There is no separator between the path and the file data.** The path's length
is carried as the `rn` prefix, and `g_put.rel` on the line below correctly uses it
(`"%.*s"`). But `rc_sync_contained(rel)` takes a bare `const char *` and scans to
the first NUL -- which `net.cpp:174` places at the end of the *whole frame*. So
the containment check runs over **the path followed by the first 8KB of the
file's contents**, as one string.

`rc_sync_contained` (`win/sync/sync.cpp`) splits on `/` and `\` and rejects any
empty component, `.`, or `..`. A `//` anywhere in that first chunk is an empty
component.

Measured, running the guest's real logic:

```
path=src/main.c  content=/* a C comment */      -> contained(rel)=1   (ok)
path=src/main.c  content=// a C++ comment       -> contained(rel)=0   REFUSED
path=web/app.js  content=// SPDX-License        -> contained(rel)=0   REFUSED
path=notes.txt   content=see http://example.com -> contained(rel)=0   REFUSED
path=readme.md   content=plain ascii            -> contained(rel)=1   (ok)
```

In every refused case `g_put.rel` is a perfectly valid path and
`rc_sync_contained(g_put.rel)` returns 1. The check rejects the content, and
blames the name.

**It does not fail open.** `rc_sync_contained` scans left to right and returns 0
at the first bad component, so the path is fully checked before any content byte
is reached -- appended data can only cause a rejection, never permit an escape.
The escape defence the function exists for is intact; what is broken is that it
also rejects a large class of ordinary files.

**The failure scenario.** The agent edits any file whose first 8KB contains `//`
-- a `//` comment in C++, Java, JavaScript, Go, Rust or a shader; any `http://`
or `https://` URL in a README, a config file or a comment; a `s//.../` in a
script. `push` gets `bad_path` (`syncrun.py:318-321`), appends a report and
returns False **without advancing the baseline**. The pass runs every thirty
seconds, re-decides `PUSH`, and fails identically, forever. `reporter.py`'s
fingerprint suppresses the repeat, so the user is told once, in the form
`push web/app.js refused: bad_path` -- which points at the filename. The edit
never reaches the machine and the honest-looking diagnosis sends the reader
after the wrong thing.

`do_get` (`syncrpc.cpp:422`) and `do_mkdir` (`syncrpc.cpp:518`) call
`rc_sync_contained(f->body)` and are **correct** -- their bodies carry only the
path, NUL-terminated by the reader. `do_put` is the only handler where data
follows the path, and the only one that gets it wrong.

**Nothing anywhere exercises this code path**, and the three things that look
like they would each stop one step short:

- **`fakegui.py` never parses a `SYNCPUT` body.** `bridge/devtools/fakegui.py:421`
  answers every `SYNCPUT` and `SYNCPUTEND` with a canned
  `SYNCPUTACK status=readonly`, by deliberate design -- the comment at `:411`
  says these replies exist only so an out-of-order frame gets an honest answer.
  So `test_fakegui_conformance.py` cannot reach it.
- **`synccheck.exe` tests `rc_sync_contained` correctly, in isolation.**
  `tests/guest/synccheck.c:93` calls it over a table of **path-only** cases. The
  function is not what is broken.
- **`rpccheck.exe` only classifies the frame.** `tests/guest/rpccheck.c:107-108`
  asserts `SYNCPUT` and `SYNCPUTEND` land in `RC_LANE_STREAM`, and stops there.

`bad_path` appears in the whole Python suite once, at
`tests/python/server/test_fakegui_conformance.py:251`, for `SYNCMKDIR`.

The defect is in the **call site** -- a correct function handed the wrong
string -- and every test aims at either the function or the frame routing. That
is the general shape worth taking away from this finding: the components are
well covered and their composition is not tested at all, because the only
component that could compose them is a fake that declines to.

**Why it has survived this long.** The check has been on that line since the
sync layer was written -- `git log -L` shows it untouched through both `756ef65`
and `dff7461`, which edited the lines directly above and below it. The reason it
has never fired in this project's own use is that the tree it syncs is this
project: **0 of the 66 sources under `win/` use a `//` comment**; the codebase is
uniformly `/* */`. The one corpus that would have caught it is the one corpus
that cannot. That is worth recording independently of the bug, because it is a
general hazard -- the guest's file handling is exercised almost exclusively
against C89-styled C, and a user syncing a JavaScript or Go project is on
genuinely untested ground.

**Fix:** validate the bounded path, not the pointer. Build `g_put.rel` first,
then `if (!rc_sync_contained(g_put.rel))`. Two lines swapped. Add a wire test that
pushes `// comment\n` and asserts `ok`.

---

## 05.2 — MEDIUM — `Baseline.save` deletes the baseline before writing the new
one, and the backup it leaves is read by nothing

`bridge/files/sync.py:365-372`:

```python
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(blob); fh.flush(); os.fsync(fh.fileno())
if os.path.exists(path):
    os.replace(path, path + ".prev")
os.replace(tmp, path)
```

The docstring above it says the write is *"Atomic, with the previous generation
kept"*, and gives the reason: *"One power cut would destroy the only record
distinguishing 'here but not there' from 'deleted there'."*

The sequence is not atomic. Between the two `os.replace` calls **`path` does not
exist**. A crash, a SIGKILL or a container stop in that window leaves no
baseline, and `Baseline.load` (`sync.py:375-381`) returns an **empty** one for an
absent file -- silently, two lines under a docstring that says *"Raises on a
corrupt baseline rather than returning an empty one. 'No baseline' is a first
sync, which conflicts the entire tree. Refusing to run is the safe failure;
guessing is not."* It refuses the corrupt case and guesses the absent one.

`path + ".prev"` -- the copy that would fix it -- is **written by `save` and read
by nothing**. Grepping `bridge/` for it returns only the write at `sync.py:371`;
the sole reader anywhere is `tests/python/files/test_sync_diff.py:294`, asserting
it exists. There is no recovery path, and nothing tells an operator it is there.

**The consequence is not a clean first sync.** With `base.rows` empty, `diff()`
falls to rows 7-10 (`sync.py:531-545`): identical files `ADOPT` harmlessly,
differing files become `CONFLICT` ("first-sync collision: no common ancestor"),
and -- the damaging one -- **every path the user deleted on the guest that the
bridge still holds becomes `PUSH, "new on the bridge"`**. The deletions all come
back, in one pass. That is precisely the resurrection failure the row-3 comment
at `sync.py:474-487` describes ("watching it reappear until you give up... the
'never asked twice' rule failing"), applied tree-wide.

The window is small but it is entered **far more often than once per pass**:
`Reporter.note` (`reporter.py:67-85`) calls `base.save()` once per distinct
notice, and its own comment acknowledges the granularity is wrong.

Two smaller defects in the same function: the directory is never fsynced, so the
rename is not durable against a host crash even when the ordering is right; and
`.prev` accumulates no generations despite `Baseline.generation` existing.

**Fix:** `os.replace(tmp, path)` first -- that single call *is* atomic and never
leaves the path absent -- then copy the old content aside if a `.prev` is wanted.
Make `load` distinguish "no file, genuinely first run" from "no file, but a
`.prev` exists", and refuse in the second case, as the docstring already promises.

---

## 05.3 — MEDIUM — `rc_sync_commit` joins without checking, and is safe only
because two buffers happen to be the same size

`rc_sync_join`'s contract (`win/sync/sync.h`) is explicit: *"Returns 1 if the
whole path fit in `cap`, 0 if it was truncated -- a truncated join names a
DIFFERENT file, so a caller that ignores the return value stats, opens or renames
the wrong thing with no way to find out."*

`rc_sync_commit` (`win/sync/sync.cpp`) is the function that **deletes and
renames**, and it opens with:

```c
rc_sync_join(full, sizeof(full), root, rel);     /* return discarded */
```

Its sibling `rc_sync_begin_write`, sixty lines away, does the opposite and
documents why: it measures `need = strlen(root) + 1 + strlen(rel)` from the
**inputs**, *"because the join itself truncates silently and would hide the very
thing being tested."* `rc_sync_commit` checks `strlen(full) + RC_SYNC_WORK_SUFFIX
> sizeof(aside) - 1` -- a length test on the already-truncated string, which is
exactly the hidden-by-truncation shape that comment warns against.

It is **not exploitable today**, and the reason is an accident. `full` and `aside`
are both `char[MAX_PATH]`. A truncated `full` is therefore always 259 characters,
so `259 + 11 > 259` trips and the function returns `-4`. The guard holds because
the two buffers are the same size.

Nothing states that dependency and no test covers it. The obvious future
edit -- enlarging `aside` to stop the `-4` refusals, which read like an
over-strict limit -- removes the guard silently and opens the path the comment in
`sync.h` describes: `GetFileAttributesA` on the wrong file, `MoveFileA(full,
aside)` moving an unrelated user file out of the way, `MoveFileA(tmp, full)`
writing the synced bytes over it, and `DeleteFileA(aside)` destroying the
displaced original -- then `return 0`, so the bridge advances its baseline over
content the machine never took.

**Fix:** measure from the inputs, as `begin_write` does, and check the join's
return. Four lines, and it makes the safety explicit rather than emergent.

---

## 05.4 — LOW/MEDIUM — a failed commit silently strips the read-only attribute
from the user's file

Same function. The order is:

```c
attr = GetFileAttributesA(full);
if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_READONLY))
    SetFileAttributesA(full, attr & ~FILE_ATTRIBUTE_READONLY);   /* cleared */

if (strlen(full) + RC_SYNC_WORK_SUFFIX > sizeof(aside) - 1) {
    DeleteFileA(tmp); return -4;                    /* <- never restored */
}
...
if (!MoveFileA(tmp, full)) {
    if (attr != 0xFFFFFFFF) MoveFileA(aside, full);
    DeleteFileA(tmp); return -3;                    /* <- never restored */
}
DeleteFileA(aside);
if (attr != 0xFFFFFFFF && (attr & FILE_ATTRIBUTE_READONLY))
    SetFileAttributesA(full, attr);                 /* only on success */
```

The attribute is restored on the success path only. On `-3` and `-4` the file
keeps its cleared attribute permanently, with nothing reported.

On Win98 read-only is the mechanism a user has for protecting a file, and
`-3` is the *common* failure -- `MoveFileA` fails when the target is open in
another program, which is the ordinary state of a file someone is editing. So the
sequence is: user marks a file read-only, has it open, the agent pushes, the
commit fails, the protection is gone and nobody is told. The next pass can then
overwrite it.

**Fix:** restore `attr` on every path out. A small RAII guard fits the
`rc_cpp.h` idiom already in use.

---

## 05.5 — LOW — `RC_SYNC_MAX_FILES` is a client rule that is not declared at
handshake, and the bridge quotes a number it assumed

Covered as 06.6 in the protocol report; recorded here because the consequence
lands in `syncrun.py`. `win/sync/sync.h:22` sets 2000;
`bridge/files/syncrun.py:51` hardcodes 2000 with a comment calling itself a
*"prose restatement of the guest's own RC_SYNC_MAX_FILES"*; `SYNCEND` sends
`status="too_many"` carrying no number. `maxdepth`, the neighbouring limit from
the same header, **is** negotiated. One `HDR("maxfiles", ...)` and one `_num` row
closes it.

---

## 05.6 — LOW — two guards are disabled by an absent `root=`, and the one that
survives measures a root the header layer may have corrupted

`SYNCEND` carries `root=g_root` (`win/sync/syncrpc.cpp:173`), and the guest
always sends it, so nothing below is live against this client. It is recorded
because both consequences are silent and the `caps` design exists precisely so
that other clients can connect.

**An absent `root=` disables two checks at once.** `syncrun.py:229` is
`if root and self.base.guest_root and root != self.base.guest_root:` -- an empty
`root` skips the tree-identity guard entirely, and `:235` then declines to record
one. `self.guest_root` stays `""` and is passed to `P.check`
(`syncrun.py:281`), where `root_n` becomes 0 (`paths.py:243-246`) and the
MAX_PATH budget is computed **without the root prefix**. Every path is then
measured short by the length of the root -- 22 bytes for
`C:\GASTON\WORK\PROJECT`. It fails safe rather than destructively, because
`rc_sync_begin_write` refuses over-long paths on the guest and reports
`too_long`, but the refusal arrives per file, forever, instead of once at the
handshake where the other capability mismatches are caught.

**And the root is a header value, so 06.3 applies to it.** The bridge decodes the
header block as ASCII with `errors="replace"`, and the guest escapes only `%`,
LF and CR -- so a sync root containing any byte above 0x7E arrives mangled. For a
single-byte codepage the damage is cosmetic and self-consistent: `C:\CAFÉ`
becomes `C:\CAF?`, both guards still compare equal, and `root_n` is unchanged
because one byte became one byte. **On a multi-byte codepage it is not.** A
CP932 client's two-byte character collapses to a single `?`, so `root_n`
under-measures the real prefix by one byte per such character, and the MAX_PATH
budget -- whose own comment at `paths.py:237-240` is specifically about getting
CP932 byte lengths right -- is computed from a string the wire already damaged.

**Fix:** treat an absent `root` as a refusal rather than a default (it is a
required field of `SYNCEND`, not an optional one), and close 06.3 so the value
arrives intact.

---

## What is right, and worth not breaking

- **`diff()` is exhaustive and ordered, and the order is the specification.**
  Rows 0-18 cover every `{absent, file, dir, unreadable}` × baseline combination.
  Row 0 (skip everything under a directory the reporting side could not list)
  prevents one unreadable directory announcing its whole subtree as deletions,
  and that is the kind of row that only gets written after someone hits it.
- **Direction comes from divergence, never from time.** With FAT's 2-second
  granularity and two unsynchronised clocks, "which is newer" is unanswerable,
  and the code refuses to pretend otherwise. `SETTLE_S` plus `Entry.cacheable`
  gating the cache **write** rather than the lookup is the subtle half, and it is
  correct.
- **`Entry.same_content` compares `(type, cls, crc)` and never size or mtime**,
  with the reason stated: a uniform text file has different sizes on the two
  sides by construction.
- **Both CRCs on every row.** `crc_raw` and `crc_norm` together with `dual` is
  what makes a `.gitattributes` `-text` override possible at all; sending one
  checksum made an override conflict forever, and the `dual` flag correctly
  refuses to infer the other from a file where they happen to be equal.
- **`base.prev` catches a reverted push.** Distinguishing "the user edited it"
  from "the user's stale editor buffer wrote back what our push replaced" needs
  the ancestor the push displaced, and nothing else supplies it.
- **The acp/case re-key guard** (`syncrun.py:129-139`) refuses rather than
  re-keying, because folding through a different codepage misses every row and
  reads the whole tree as deleted.
- **`if_crc` is a real compare-and-swap** carried to the guest and re-checked
  there against a fresh read, which closes the window between the manifest
  snapshot and the write.

---

# Resolution — 2026-09-22

All 6 findings (05.1 through 05.6) addressed, verified, and closed.

- **05.1 (Push path containment check):** Confirmed already fixed. `rc_syncrpc_put_path` (`win/sync/sync.cpp`) was refactored to extract and length-bound the path into `g_put.rel` before validating containment via `rc_sync_contained(g_put.rel)`, eliminating the concatenation with file payload that rejected `//` in file bodies. Verified with `synccheck --putpath` and `test_guest_syncrpc_handlers`.
- **05.2 (`Baseline.save` atomicity and `.prev` recovery):** Fixed in `bridge/files/sync.py`. `Baseline.save()` copies the existing baseline aside to `.prev` first, then atomically replaces `tmp` into `path` with `os.replace(tmp, path)` and fsyncs the parent directory, eliminating the window where `path` is absent. `Baseline.load()` now checks if `path` is missing but `path + ".prev"` exists, raising `RuntimeError` rather than returning an empty baseline (which would resurrect all deletions). Verified in `tests/python/files/test_sync_diff.py` (`test_missing_baseline_with_prev_refuses`, `test_save_is_atomic_path_never_absent`).
- **05.3 (`rc_sync_commit` join check):** Confirmed already fixed. `rc_sync_commit` in `win/sync/sync.cpp:637` checks `rc_sync_join(full, sizeof(full), root, rel)` and logs/returns `-4` if the joined path does not fit.
- **05.4 (Failed commit restores read-only attribute):** Fixed in `win/core/rc_cpp.h` and `win/sync/sync.cpp`. Introduced `ReadOnlyGuard` RAII class that strips `FILE_ATTRIBUTE_READONLY` on entry and restores it on scope exit across every exit path (`-3`, `-4`, or `0`), preventing failed commits on busy/locked files from silently clearing write protection. Verified with `readonly_ok` and `readonly_fail` test cases in `tests/guest/synccheck.c` and Python assertions in `tests/python/guest/test_guest_agreement.py`.
- **05.5 (`RC_SYNC_MAX_FILES` handshake declaration):** Fixed. Added `RC_CAP_MAXFILES` to `win/net/caps.h` and emitted `HDR("maxfiles", RC_STR(RC_CAP_MAXFILES))` in `win/net/net.cpp`. Added `maxfiles` to `Caps` dataclass in `bridge/files/caps.py` and parsed from HELLO with fallback default `MAXFILES_GASTON = 2000`. Updated `bridge/files/syncrun.py` to use `self.caps.maxfiles` in the `too_many` error message. Added to `FAKE_CAPS` in `fakegui.py`, `capscheck.c`, and verified with `tests/python/guest/test_guest_caps.py` and `tests/python/files/test_caps.py`.
- **05.6 (Absent `root=` in `SYNCEND`):** Fixed in `bridge/files/syncrun.py`. `scan_guest` now explicitly refuses with `SyncError` if `root` is absent or empty in `SYNCEND`, ensuring the tree-identity check cannot be bypassed and `guest_root` is always populated for MAX_PATH budgeting. Verified in `tests/python/files/test_sync_wire.py` (`test_absent_root_in_syncend_is_refused`).

