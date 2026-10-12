# Stage 3b — pagination: the bridge stops dividing the transcript

## Context

`docs/ROADMAP.md:300-324` is this stage. Its exit condition, from the table at `:36`:
**"the guest asks for blocks by range, and `pages.py` is an append-only transcript."**

Stage 3 made this possible and deliberately did not build it. Everything `pages.py` does
exists to cut a stream of HTML into MSHTML-sized chunks at boundaries safe for MSHTML.
After stage 3 a record is indivisible, every boundary is safe, and the client declares its
own caps — so the bridge is dividing a transcript for a layout engine it no longer renders
for, using a memory figure (`mem_kb`) it has to be told, scaled by a preference
(`view.page`) that is about how the *client* looks.

### The two facts that shape everything below

**The guest already ignores the bridge's page numbers on TEXT.** `Turn::onText`
(`win/app/turn.cpp:179-201`) reads exactly one header, `mode`. The `page`, `total` and
`off` on every TEXT frame are never read — `protocol.py:361-368` already says so for
`total`/`off` and leaves `page` to this stage. Page state lives in exactly two places on
the guest: `rc_pagebar`'s `view/total/is_live/loading_page` (`win/app/pagebar.h:59-61`)
and `rc_cache`'s `page_no` plus the `P%03d.HT2` filenames (`win/app/cache.cpp:85`).

**A page is a memory bound, not a layout idea, and that is not negotiable.**
`win/app/pagebar.h:7-9`: *"The control holds exactly ONE page, which is what bounds memory
by page size rather than by conversation length."* On a 32MB box with 28KB free after a
turn, continuous scrollback would hold the whole conversation in MSHTML. So **pages
survive this stage**. What dies is the bridge deciding them.

---

## STATUS: REPLANNED 2026-10-12, after five commits had landed

The first draft of this file was written from `ROADMAP.md`'s three sentences and the stage
was started against it. It was then put through one adversarial review round against the
code -- **27 defects, three of which killed a decision** -- and the decisions below are what
came out. Where the first draft was wrong, this says so; that is the cheapest documentation
available and it is how stage 3 was planned.

The three that were killed:

- **Deleting the PREFS `view.page` key would have refused every connection.** `prefs.py`'s
  key chain ends in `raise PrefsError`, and on the HELLO path that is fatal.
- **The guest asking after `SWITCHED` would have hung off a control that may be NULL**, and
  would have fired *during a streaming turn*, clearing the transcript.
- **A `GREW` frame per block** roughly doubles the frame count of a long turn on a link
  deliberately throttled to 5 KB/s.

## 1. The decisions

1. **The page becomes the client's own**, computed from its own memory and its own
   Small/Normal/Large setting. `Page %d/%d (Live)` is unchanged on screen.
2. **The wire carries `offset` and `limit`** (§2). `GETBLOCKS` replaces `GETPAGE`.
3. **`RANGE` + `RANGEEND` replace `PAGESTART`/`PAGEEND`; `GREW` replaces `PAGESEALED`.**
   Three for three -- `ANCHOR` already died. `RANGE`'s `count` must have a guest-side
   reader or it is the same decoration this stage is deleting: the page bar uses it to tell
   a short last window from one the bridge refused a record out of (`link.py:145-154`).
4. **After a switch the guest asks; the bridge does not push.** No new HELLO key; the count
   stays at 39. An earlier draft of this stage added a `pageblocks` key so the bridge could
   push a window after a switch, and removing it is what makes "the guest asks for blocks by
   range" literally true. **Two guards, without which this decision is wrong:**
   - **The ask may not hang off the chat list.** `SWITCHED` is dispatched to
     `MainWindow::onChats` and reaches `onSwitched` only through `if (m_chats)`
     (`mainwindow.cpp:542`). `m_chats` is allowed to be NULL -- controls do fail to create on
     a 64KB USER heap. Today a NULL chat list costs a page total; under this decision it
     would cost the whole transcript after every switch, forever. The ask goes in the
     dispatch wrapper, beside the handler, not inside the control.
   - **The ask fires only when the conversation actually changed.** `SWITCHED` is not only
     an attach frame: `session.py:492-494` sends it from `resume_pending` on the FIRST
     MESSAGE of a new conversation, and `turn_runner.py:236-240` calls that *before* taking
     `turn_lock`. An unconditional ask would put a `RANGE` -- which runs `turnDone()` and
     `clear()` -- into the middle of a streaming turn.
5. **`offset=live` aligns to the grid the request's own `limit` implies** --
   `(count - 1) // limit * limit`. It cannot mean "the last `limit` records": with 200
   records and a limit of 90 that window starts at 110, which is not where any of the
   guest's pages begin. The arithmetic follows from the limit the guest supplied, so it is
   not an opinion about what a page is.
6. **The page-size preference stops being READ by the bridge. It does not stop being
   ACCEPTED.** `bridge/prefs.py:161-163` ends its key chain with `raise PrefsError(key, "not
   a setting this bridge knows")`, and on the HELLO path that is fatal --
   `connection.py:203-207` answers `bad_prefs` and drops the socket. The client sends
   `view.page` unconditionally (`win/app/protect.cpp:221`). Deleting the branch would refuse
   every connection from any guest not reflashed in lockstep, permanently, and a PREFS key
   can therefore never be removed -- only ignored. `limits_for`, `prefs.scaled`,
   `PAGE_SCALE`, the ceiling constants and `Handshake.page_bytes/page_blocks` do die (§5).
7. **`PROTO_VERSION` is bumped to 3.** Both sides are `2` today
   (`bridge/wire/protocol.py:36`, `win/net/frameproto.h:13`) and `handshake.py:87-90`
   refuses a mismatch cleanly. Without a bump an un-reflashed guest connects, sends
   `GETPAGE`, gets `unknown_type`, and shows a transcript that appends but cannot page --
   working badly rather than refusing. This stage breaks the wire; the version says so.
8. **How the guest learns the transcript grew -- two cases, not one.** Following live, an
   APPEND TEXT frame is already going out, so the count rides on it as a `total` header:
   `page` and `off` die, `total` returns meaning *how many records the transcript holds*,
   which is a different quantity from the page count it used to carry. Paged back, no TEXT
   is sent (`write_block` streams only when `view_live`), so `GREW` goes on its own.
   A `GREW` in both cases was the first draft, measured at roughly double the frame count of
   a long turn, each frame its own `write` + `drain`, and one `SetWindowTextA` plus five
   `EnableWindow` calls on the guest per block to redraw the same string. The guest counting
   its own records is rejected in the other direction: a count that drifts from the bridge's
   is wrong SILENTLY, and this tree has paid once for an invisible divergence in `.thinking`.
9. **A window is delivered under a lock, and `view_live` is set after it.** `on_getblocks`
   runs in the reader task and `write_block` in the turn task (`server.py:456`), and
   `send_range` awaits per chunk -- so a block appended mid-delivery is streamed BETWEEN the
   window's own records, on screen and into the cache file. The per-frame page tag that
   would have made that diagnosable is being deleted in this same stage, which is the
   argument for fixing it here rather than meeting it later.
10. **An empty WINDOW is not an empty CONVERSATION.** `view_live = offset + limit >= count`
    is true for every offset past the end, which would put "Type a message below to start
    this conversation" on a conversation that has records in it. The notice fires on
    `count == 0` only; a window's emptiness is reported by `count=0` and nothing else.
11. **The cache keeps `P%03d.HT2`** (§4) and gains two honesty fixes the byte cap used to
    hide: `end_page` refuses to publish a page over `CACHE_PAGE_MAX`, and `restoreCache`
    stops claiming the file it shows is the last page of the conversation.
12. **`send_range` chunks on record boundaries**, like the export: a window is `limit`
    records of up to `maxblock` bytes each, so it is not frame-bounded the way a 16KB page
    was. 90 blocks of 60KB is 5MB.

### Why `ANCHOR` got no replacement

`anchor_frame(page, off, preview)` existed so the guest could jump back to a user turn. The
guest's handler wrote a log line and nothing else. An anchor keyed on a page plus a byte
offset within that page has no meaning once the client picks the page, and rebuilding it as
a block index would be adding a feature in the stage that is removing one. `BACKLOG.md` gets
the shape it would come back in.

## 2. The wire

```
guest  -> GETBLOCKS  offset=<n>|live  limit=<k>
bridge -> RANGE      offset=<n> count=<c> total=<t> live=0|1
bridge -> TEXT       mode=append  (zero or more frames of whole records)
bridge -> RANGEEND

  then, as the transcript grows -- one of these, never both:

bridge -> TEXT       mode=append total=<t>  (the guest is following live)
bridge -> GREW       total=<t>              (the guest has paged back)
```

- **`offset` is a block index, zero-based.** Zero-based because it is an index into a list
  and the list is the bridge's; the page *numbers* the client shows are one-based and the
  client's own business. Mixing the two conventions in one head is how off-by-ones happen,
  so they are kept apart by being different words: `offset`/`count` on the wire, `page`
  on screen.
- **`count` is how many records actually follow**, which is not always `limit`: the last
  range is short, and `offset` past the end gives `count=0`.
- **`live=1`** means this range ends at the current end of the transcript, so new blocks
  will stream into it. The bridge sets it; the guest does not infer it from the
  arithmetic, because the transcript can grow between the request and the answer.
- **`TEXT` loses `page` and `off` and keeps `total` with a new meaning.** `page` was sent
  on every frame and `Turn::onText` never read it; `off` was dead from stage 3 C2. `total`
  comes back as **how many records the transcript holds** -- a different quantity from the
  page count it used to carry, which the docstring has to say out loud, because a header
  that keeps its name and changes its meaning is the worst kind of wire change. It is on
  APPEND only: a LIVE frame replaces the open block and does not grow the transcript.
- **Nothing is sealed**, so a record that does not fit a frame is the same case the export
  already handles: `link.py` sends it in a frame of its own.

- **`GREW` carries one header and is sent only to a guest that has paged back.** It is
  what `PAGESEALED` did, without the page: the only way a reader who is not following live
  learns the conversation moved on, because no TEXT goes to them at all.

**`maxblock` still bounds one record** (`win/net/caps.h:163-173`) and keeps its meaning.
The comment there says it bounds "the arm that replaces the paginator's mid-run split"; the
paginator goes, the bound does not, and the comment is rewritten rather than deleted.

---

## 3. What `pages.py` becomes

```python
class Transcript:
    records: list[bytes]
    def add(self, record: bytes) -> int        # returns the index it landed at
    def count(self) -> int
    def range(self, offset: int, limit: int) -> list[bytes]
    def all_records(self) -> list[bytes]       # the export, unchanged
```

Deleted with their reasons: `Page`, `PageRun`, `_seal`, `continued` (already always
false), `MAX_BYTES`, `MAX_LINES`, `MAX_BLOCKS`, `HARD_BYTES`, `HARD_LINES`,
`HARD_BLOCKS`, `over_soft_cap`, `over_hard_cap`, `over_hard_blocks`, `message_boundary`,
`limits_for`, `Anchor`, `mark_user_turn`, `Paginator.add`, `Paginator.all_text`,
`Paginator.anchors`, `Paginator.page`, `Paginator.live`, `Paginator.count`.

**One latent inconsistency is deleted rather than fixed**, and it is worth naming because
it is evidence for the stage: `session.py:586` calls `guest.view.pages.live.over_soft_cap()`
**with no arguments**, so the history replay seals against the module defaults rather than
against the guest's scaled caps. A 128MB client's replay is cut at a 32MB client's page
size. Nobody noticed because the only symptom is a page break in the wrong place.

---

## 4. The cache, which is the one place a wrong answer loses the person's words

`win/app/cache.cpp` writes one file per page, named `P%03d.HT2` after the page number, and
`page_count()` (`:235-248`) is a dense existence scan from 1. Stage 3 changed what is
*in* those files and deliberately put the version in the filename rather than using a
FORMAT file, because `mainwindow.cpp:620-631` reads the cache before anything can refetch:
a discard-on-upgrade would have deleted the conversation on the first cold start after the
upgrade, with nothing to re-fetch from.

**The same argument applies again, and the answer is that nothing changes.** The number in
the filename is already, in effect, the client's page index — the client just used to be
told it. A cached file holds a run of records and stays a valid run of records whatever the
page size becomes. So:

- **No format change, no new version, no discard.** `P%03d.HT2` keeps its name.
- **A page-size change does not invalidate the cache.** The boundaries of the cached files
  may no longer match the size the client now wants; the only consumer of those boundaries
  is `restoreCache` (`mainwindow.cpp:583-629`). Showing a differently-sized page in an
  outage is not worth deleting a conversation over -- but `page_count()` is a CONTIGUOUS
  scan from `P001` (`cache.cpp:234-246`), so after a size change it returns a file written
  at the old size while the notice says "This is the last page of your conversation as it
  was cached here". **The notice stops claiming that**, and names what it is instead. The
  cache was always allowed to be stale; it was not allowed to lie about it.
- **`end_page` refuses to publish a page over `CACHE_PAGE_MAX`** (64KB, `cache.cpp:19`).
  With `MAX_BYTES` gone (§5) a window is bounded only by record COUNT -- up to 360 -- times
  record size, up to 64,512 each, and `load_page` already refuses an oversize file at READ
  time (`cache.cpp:215-218`). Without this the cache would write pages it can never read
  back, and `restoreCache` would promise a page and show nothing. Refusing at write keeps
  the cache readable and the gap visible to `page_count`.
- **What moves is the write trigger.** `begin_page`/`end_page` hang off PAGESTART/PAGEEND
  today (`mainwindow.cpp:639-652`); they hang off RANGE/RANGEEND after, with the page
  number the client's own.

---

## 5. The client's page size

`limits_for(mem_kb)` (`pages.py:127-143`) scales 16KB/90 blocks by `mem_kb / 32MB`, clamped
to [1,4]. `prefs.scaled` then applies Small 0.5 / Normal 1.0 / Large 2.0 and clamps to a
ceiling of four times the 32MB page. The client knows `mem_kb` — it is the one that
measured it (`win/net/net.cpp:348-352`) — and it knows the preference, so **both
computations move into the client unchanged**, as one function in `win/app/pagebar.cpp`.

**The declaration does not move with it.** An earlier draft of this stage sent the result
as a `pageblocks` HELLO key so the bridge could push a window after a conversation switch;
decision 4 made the guest ask instead, which is what leaves the bridge with no page-size
number at all. Every `GETBLOCKS` carries its own `limit`, and `offset=live` aligns to the
grid that limit implies -- so the only arithmetic left on the bridge follows from the
request in front of it.

**One behaviour changes and the options tooltip has to say so:** `rc_net_page_blocks`
returns a value filled in `rc_net_start` (`win/net/net.cpp:300-304`), so changing
Small/Normal/Large takes effect at the next connect rather than at the next page.

The byte half (`MAX_BYTES`) does not move. It existed because a page was a string of HTML
and MSHTML had to hold it; a range is a count of blocks and the bound that matters is
`maxblock` per record and the block count per page. Dropping it is the stage's one
behavioural change the person could notice: a page of 90 very long blocks is now allowed
where it used to be cut at 16KB. **That is the thing to measure on the 32MB VM** (§8),
and the fallback is a byte cap the client applies to its own `limit`.

---

## 6. The oracle, and what replaces the proof

`bridge/view/htmlrender.py` is the only file on the allowlist in
`tests/python/meta/test_no_markup_on_the_bridge.py`, with two tests that exist to make its
deletion loud. It dies here with `HtmlStream`, `Update`, and `html.py`'s re-exports.

`tests/python/guest/test_render_equivalence.py` compares `rendercheck --render` under Wine
against `htmlrender.render`, byte for byte. With no oracle there is nothing to compare to,
so the expected bytes are **generated from the current, proven output and checked in**, and
the test becomes a fixture comparison. Stated plainly, because a fixture is weaker than a
proof: it pins the renderer against *what it does today* and can no longer catch a renderer
and an oracle that drift together. It can still catch any change to the guest's output,
which is what it is for after this.

The fixture is generated **in the same commit that deletes the oracle**, from a run of the
proof that passed, so there is never a commit where neither exists.

---

## 7. Commits

Five landed before the replan: the plan doc, `rendercheck` joining `guest-checks`,
`Transcript`, the dead export loop, and the anchors. What is left:

- **B2 -- the cutover.** One commit, because the two sides cannot disagree for even one.
  It is a rework of work that was already written: remove the `pageblocks` HELLO key and
  `Caps.pageblocks` (decision 4), keep `rc_page_blocks` but stop declaring it, put the ask
  on a real conversation change from the dispatch wrapper (decision 4's guards), ride
  `total` on APPEND TEXT (decision 8), add the delivery lock (decision 9), fix the empty
  notice (decision 10), bump `PROTO_VERSION` (decision 7).
  **B2 also owns everything decision 6 touches**: `page_bytes`/`page_blocks`/`limits_for`/
  `prefs.scaled` and BOTH production callers -- the handshake and `options.py:129-130`, the
  PREFS handler, which the first draft left unassigned and which has a test written to catch
  exactly that (`test_presentation.py:139`).
  **Nine goldens regenerate, not twelve**; 03, 07 and 09 carry no page headers. While there:
  `scenario_07_conversation_switch` is a DEAD golden -- a lone `unknown_type` ERROR, because
  the test sends `Frame("NEW", {})` against a dispatch table that says `NEWSESSION`
  (`test_frame_goldens.py:300`). It is the one scenario that should cover decision 4's new
  switch flow and it asserts nothing.
- **B3 -- the paginator's remains.** It owns `Paginator`'s production importers too
  (`server.py:49,63-71,359`, `connection.py:25,294`, `transcript.py:30,51,93`): the first
  draft deferred them and would have made this commit an `ImportError` at `import
  bridge.server`. Three test files construct it, not one. Of `test_pages.py`'s 30 tests the
  ones about the transcript rather than about cutting it move to `test_transcript.py`.
- **C1 -- the oracle dies** (§6). Seven imports across six files, two module-level, and the
  48-test file the commit exists for is `test_render_equivalence.py`. `html.py`'s block
  constructors STAY -- nine production modules and 32 `H.x(...)` calls hang off them, and
  moving those is a different commit. The fixture is generated here, from a passing run,
  before the oracle goes, and does not use `golden_utils.assert_matches_golden`, which
  writes a missing golden silently (`golden_utils.py:95`).
- **C2 -- docs and the measurements.**

## 8. Verification

- **Wine proves the code**: the whole Python suite, `rendercheck`'s 76 cases, the fixture.
- **The VM proves the platform**: a conversation long enough to page (the thing no unit
  test can produce), paging back and forward, the cache after an outage with no bridge, a
  page of long blocks against the dropped byte cap (§5), and an export.
- **The Athlon proves the CPU**: not needed by this stage. It stays off unless §5's
  measurement says the byte cap has to come back, which is a render-time question.
- **Every image-moving commit**: `make -C win check` with the committed number in the
  message. There is **2,048 bytes of slack** (`496,377` of `498,425`) and this stage should
  give some back.
