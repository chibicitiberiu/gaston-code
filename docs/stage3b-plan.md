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

## 1. The decisions

1. **The page stays, and becomes the client's own.** The client computes how many blocks a
   page holds from its own memory and its own Small/Normal/Large setting, and asks for that
   many. `Page %d/%d (Live)` is unchanged on screen; the arithmetic behind it stops being
   the bridge's.
2. **The wire carries `offset` and `limit`.** `GETBLOCKS` replaces `GETPAGE`. `offset` is a
   block index or the literal `live`; `limit` is a count. The bridge has no opinion about
   what a page is, which is the whole point of the client declaring its own caps.
3. **Three frames replace four.** `RANGE` (`offset`, `count`, `total`, `live`) and
   `RANGEEND` replace `PAGESTART`/`PAGEEND`; `GREW` (`total`) replaces `PAGESEALED`;
   `ANCHOR` dies with nothing in its place.
4. **The page-size preference stops crossing the wire.** `view.page` stays in `gaston.ini`
   and the options sheet, because the person still chooses how much a page holds. It stops
   being a PREFS key, and `limits_for`, `prefs.scaled`, `PAGE_SCALE`, the ceiling constants
   and `Handshake.page_bytes/page_blocks` all die with it. This is `CLAUDE.md`'s *the
   client declares OS facts; the bridge holds none*, applied to the last place it was not.
5. **The cache's format does not change**, and that is a decision rather than an accident
   (§4).
6. **The oracle dies, and the equality proof becomes a checked-in fixture** in the same
   commit, so there is never a window with neither.

### Why `ANCHOR` gets no replacement

`anchor_frame(page, off, preview)` (`protocol.py:284-291`) exists so the guest can jump
back to a user turn. The guest's handler is `MainWindow::onAnchor`
(`win/app/mainwindow.cpp:662-668`) and it **writes a log line**. Nothing else in `win/`
touches it. An anchor keyed on `page` + a byte offset *within that page* has no meaning
once the client picks the page, and rebuilding it as a block index would be building a
feature nobody has asked for in the stage that is removing one. It goes, and
`BACKLOG.md` gets the entry: a block index is the shape it would come back in.

### What a `GREW` frame is for

Today `PAGESEALED` does two jobs: on the live page it follows the seal forward
(`pagebar.cpp:236`), and on an older page it is the only signal that new content exists.
The first job disappears — there are no seals. The second is real: without it, "Page 3/7"
stays "3/7" while the conversation grows, and the person cannot tell. `GREW` carries one
header and is sent only when the guest is not following live.

---

## 2. The wire

```
guest  -> GETBLOCKS  offset=<n>|live  limit=<k>
bridge -> RANGE      offset=<n> count=<c> total=<t> live=0|1
bridge -> TEXT       mode=append  (zero or more frames of whole records)
bridge -> RANGEEND
bridge -> GREW       total=<t>              (only while the guest is paged back)
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
- **`TEXT` keeps `mode` and loses `page`.** `total` and `off` were already dead and go
  with it. `text_frame`'s signature loses three parameters.
- **Nothing is sealed**, so a record that does not fit a frame is the same case the export
  already handles: `link.py` sends it in a frame of its own.

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
  is `restoreCache` (`mainwindow.cpp:583-629`), which shows the *last* cached file and says
  so. Showing a slightly differently-sized last page in an outage is not worth deleting a
  conversation over.
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

**Phase A — nothing on the wire changes.**

- **A0** this plan, and the stage numbers stage 3's renumbering left stale (R7 is 3c, not
  3b, in five places). No code.
- **A1** **`rendercheck` joins `guest-checks`.** It is in `win/Makefile`'s `all`
  (`:629`) and has its own target (`:431`), but it is **not** in the root `Makefile`'s
  `guest-checks` list (`:93-95`) — so `make clean && make wine-smoke` does not build it and
  `conftest.guest_exe` fails all 69 tests in `test_render_equivalence.py` and
  `test_guest_render.py`. It passes today only because somebody ran `make -C win all`.
  This is the same hand-maintained-list gap the comment at `Makefile:77-80` was written
  about, and the same class as stage 3's `capscheck` break. It goes first, because
  everything after it leans on those two suites.
- **A2** `Transcript` lands beside `Paginator`, which delegates to it. Behaviour identical.
- **A3** `Paginator.all_text` deleted, its two callers re-pointed
  (`test_server_integration.py:2617`, `test_session.py:39`). No production caller, and it
  is one of the two remaining production imports of the oracle.
- **A4** `link.send_export` deleted — the byte-oriented chunk loop, no caller anywhere.

**Phase B — the wire changes, one deletion at a time.**

- **B1** anchors die: `Anchor`, `mark_user_turn`, `anchor_frame`, `turn_runner`'s send,
  the guest's `onAnchor` and its dispatch row, fakegui's handler, the `ANCHOR` frames in
  goldens 02/03/08/11, and `golden_utils.py:42-45`'s preview mask.
  `test_protocol.py:314-338` uses `Frame("ANCHOR", ...)` only as a vehicle for four
  header-encoding tests — the frame name changes, the tests do not.
- **B2** **the cutover, one commit because the two sides cannot disagree for even one.**
  `GETBLOCKS`/`RANGE`/`RANGEEND`/`GREW`; `Transcript.range`; `send_page` becomes
  `send_range`; `text_frame` loses `page`/`total`/`off`; `pagebar.cpp` computes its own
  pages and asks by range; the cache's write trigger moves; `fakegui` moves; **all 12
  goldens regenerate** (23 TEXT frames lose `page=`, READY's `page`/`total` go in five, and
  `scenario_06_page_request_and_seal` is rewritten as a range request).
  `test_server_integration.py:279-460`'s six end-to-end pagination tests are rewritten
  against ranges — they are the behavioural proof and they do not die.
- **B3** the paginator's remains: `Page`, `PageRun`, `_seal`, the three `over_*`,
  `message_boundary`, `HARD_*`, `MAX_*`, `limits_for`, `prefs.scaled`, `PAGE_SCALE`,
  `Handshake.page_bytes/page_blocks`, the PREFS `view.page` key on both sides, and
  `test_pages.py`'s 29 tests — of which the ones that are about *the transcript* rather
  than about cutting it (forward numbering, whole records, an oversize record) move to a
  new `test_transcript.py` rather than dying.

**Phase C — the seam closes.**

- **C1** the oracle dies. `htmlrender.py`, `HtmlStream`, `Update`, the `html.py`
  re-exports, `test_html.py`'s 35 tests (the parser half already lives in
  `test_blocks.py`'s 58), the allowlist and its two guard tests, and the five function-local
  oracle imports in `test_guest_css.py`/`test_mode_descriptions.py`/`test_block_vocab.py`/
  `test_permissions_gaston.py`, which re-point at `rendercheck` or at the record payload.
  **The fixture is generated in this commit, from a passing run, before the file goes** —
  ~72 KB over five blobs, the LIVE sample dominating. It does **not** use
  `golden_utils.assert_matches_golden`: that writes a missing golden silently
  (`golden_utils.py:99`), which for this fixture would mean a deleted file is re-blessed
  from the guest against itself. `bridge/server.py:__all__` loses `H`, `APPEND`,
  `Paginator`, and `test_suite_hygiene.py:174-215` is what catches it if they are missed.
- **C2** docs: `ROADMAP.md` 3b done with its numbers, `BACKLOG.md` the anchor entry and
  §13/§31 final, `PROGRESS.md` the stage entry, the measurement from §8.

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
