# 06 — The client/bridge wire protocol

Audit date 2026-09-21. Scope: `bridge/wire/protocol.py`, `bridge/wire/link.py`,
`bridge/wire/fold.py`, `bridge/files/caps.py`, `bridge/handshake.py` against
`win/net/frameproto.{h,cpp}`, `win/net/net.cpp` and `win/net/caps.h`.

Method: both sides read line by line, and every claim below was executed rather
than argued. Where a finding says "measured", it was produced by running the
project's own code -- the bridge's real `Frame`/`FrameReader` against
byte-for-byte reproductions of what `rc_frame_build` emits.

**The protocol is well designed.** The format is small enough to parse with
`strchr`, the framing has no resync path and says so, the lane table is a table,
and nearly every constraint carries the incident that produced it. The findings
below are all in one place: **the two sides do not agree on what a header value
is**, and the disagreement is invisible because each side is internally
consistent.

`frameproto.h:1` says *"Mirror of bridge/protocol.py -- keep in sync."* That is
the claim under test.

---

## 06.1 — HIGH — `MAX_HEADER_VALUE` is a documented protocol invariant enforced
at one of ~40 producers, and violating it drops the connection

`bridge/wire/protocol.py:36` defines `MAX_HEADER_VALUE = 255` and documents the
consequence precisely: a longer value *"is REFUSED by the guest's parser, which
drops the whole frame."* `win/net/frameproto.cpp:156` is that refusal
(`rc_strcpy(h->val, RC_MAX_VAL, ...)` returning false → `return -1`), and
`win/net/net.cpp:184-186` turns a parse failure into `"malformed frame, dropping
link"` followed by `break` out of the reader loop.

So: **one over-long header value anywhere = the guest silently disconnects.**

`Frame.encode()` (`protocol.py:110-133`) does not check it. Measured:

```
a 304-byte header value encodes fine, length 321 - no refusal
```

The constant is referenced at exactly one call site in the entire bridge --
`bridge/server.py:2331`, for `exec`'s `cwd` -- and nowhere else. The producers
that can exceed it with ordinary data:

| producer | header | bound today |
|---|---|---|
| `protocol.py:236` `error(code, msg)` | `msg` | **none** |
| `protocol.py:449` `option(...)` | `desc` | **none** |
| `server.py:3486` `option("work", ...)` | `id` and `desc` = `guest.work_guest` | **none**, a full guest path |
| `protocol.py:286` `session_row` | `title`, `cwd` | `[:120]` *characters*, not encoded bytes |

**`error()` is the sharpest.** `server.py:634` sends
`error("protocol", str(exc))` from the read loop's `ProtocolError` handler, and
`Frame.decode` builds those messages out of the offending data:
`f"header line without '=': {line!r}"`, where `line` can be most of a 64KB frame.
`server.py:1712` sends `error("unknown_type", f.type)` and `Frame.decode` puts no
bound on the type either (`lines[0]`, `protocol.py:147`) where the guest caps it
at `RC_MAX_TYPE` 32. So a malformed frame produces an error frame that is itself
undeliverable, and the guest's diagnosis of "the link died" is all the user gets.

Measured, feeding the bridge's own `FrameReader` a frame desynced the way a
flaky NE2000 link desyncs one:

```
decode raised, message length: 3027
error() frame encodes OK, total 3057 bytes
  msg= header value is 3027 bytes on the wire (guest cap is 255)
  -> guest rc_frame_parse returns -1 -> net.cpp drops the link

bridge accepted a frame type of length 5000 (guest caps types at 32)
  error("unknown_type", f.type) would encode a 5034 byte frame
```

Neither `encode()` nor anything above it objects. The link drops, and the frame
explaining why is the thing that drops it.

This is the same bug `turn_error` was fixed for. Its docstring
(`protocol.py:330-346`) explains it exactly -- *"the longer and more informative
the error, the more likely the user saw a disconnection instead of the error"* --
and moved the text to the body. `error()`, one screen above it in the same file,
still has it in a header.

**`option("work", ...)` is the one reachable without anything going wrong.**
`guest.work_guest` is a guest path, and both `id` and `desc` carry it in full. A
work directory at or past 256 characters -- legal under `MAX_PATH` 260 -- drops
the OPTION frame, and with it the toolbar's work button.

**Fix:** enforce in `Frame.encode()`, where every frame already passes. Refuse or
truncate-with-a-marker, but do it once, in the encoder, rather than at 40 call
sites that must each remember. The `MAX_FRAME` check is already there, four lines
away, and is the model to copy.

---

## 06.2 — HIGH — the one place that does check measures the wrong length, and
can under-measure by 3×

`bridge/server.py:2331`:

```python
if len(cwd.encode("utf-8", "surrogateescape")) > MAX_HEADER_VALUE:
```

The limit is on the **percent-encoded CP1252 bytes on the wire**, not on UTF-8.
`_quote` (`protocol.py:66-71`) folds to the connection's codepage and then
expands every byte outside `0x20..0x7E` to three characters.

For a path of accented Latin characters -- `C:\Mes Documents\Café\...`, entirely
ordinary -- `é` is 2 bytes in UTF-8 and 3 on the wire (`%E9`). The check passes at
255 UTF-8 bytes; the wire value is up to 381. The guest drops the frame, and
because this is `EXEC`, the model is left waiting on a command that was never
delivered while the link goes down underneath it.

**Fix:** measure what is sent. `len(_quote(cwd))`, or better, let 06.1's
encoder-level check be the only one and delete this.

---

## 06.3 — MEDIUM — the two sides implement different escaping rules, and
guest→bridge is lossy

`frameproto.h:6-8` states the format: *"Header values are percent-encoded for %,
\n and \r only, so parsing is strchr and nothing more."* The guest's writer
(`append_quoted`, `frameproto.cpp:246-266`) implements exactly that, and its
comment ends *"Everything else goes through untouched."*

The bridge does something else. `_SAFE` (`protocol.py:57`) is
`0x20..0x7E` minus `%`, so `_quote` escapes **every** byte outside printable
ASCII -- and `Frame.decode` (`protocol.py:141`) reads the header block with
`.decode("ascii", errors="replace")`.

So a byte the guest is documented to pass through untouched is a byte the bridge
cannot represent. Measured, using the bridge's own `FrameReader` on the exact
bytes `rc_frame_build` produces:

```
guest sent header bytes : b'C:\\caf\xe9'
bridge decoded it as    : 'C:\\caf?'

bridge->guest same text : b'SESSION\ntitle=caf%E9\n\n'
```

The `\xe9` survives bridge→guest and does not survive guest→bridge. It is not an
exception path: `errors="replace"` means it is silent, and `_unquote` then folds
the `U+FFFD` to `?` on the way through.

Reachable with ordinary data. `HDR("sync_root", ...)` (`net.cpp`) sends the
configured sync root from the INI, and `SYNCEND` carries `root=`. A user whose
folder is `C:\CAFÉ` has it recorded in `Baseline.guest_root`
(`bridge/files/sync.py`), printed in `sync_status`, and written into the agent's
system prompt as `C:\CAF?`.

It does **not** currently corrupt file contents or filenames: manifest rows and
`SYNCPUT` paths travel in frame **bodies**, which are raw bytes and are decoded
through the declared ACP. The exposure is confined to headers. That is luck of
layout rather than design, and the next header carrying a filename inherits it.

**Fix:** make `append_quoted` escape everything outside `_SAFE`, matching
`_quote`. It is a three-character change to the `if` and it makes the header file's
"mirror" claim true. Then `Frame.decode`'s `errors="replace"` can become
`errors="strict"`, so a future divergence raises instead of substituting.

---

## 06.4 — MEDIUM — the guest's strictness limits are not mirrored on the bridge,
so the bridge will accept and re-emit what it cannot deliver

The guest refuses, in `rc_frame_parse`: a type ≥ `RC_MAX_TYPE` (32), more than
`RC_MAX_HEADERS` (16) headers, a key ≥ `RC_MAX_KEY` (32), a value ≥ `RC_MAX_VAL`
(256), and a duplicate key. Each refusal is a dropped frame and therefore a
dropped link, and each is deliberate and commented.

`Frame.decode` enforces exactly one of those five: the duplicate key
(`protocol.py:157`). It bounds neither type, count, key nor value.

The asymmetry has two consequences. A guest that is confused or corrupt sends a
frame the bridge accepts and then echoes fragments of into `error()` headers
(06.1). And nothing on the bridge fails when a *bridge-side* change produces a
frame over one of the guest's limits -- the first symptom is a disconnect on the
real machine.

`tests/python/guest/test_guest_frame.py` counts headers from `protocol.py` so a
seventeenth fails there (`frameproto.cpp:130-140` says so). That covers the
count. The other three limits have no equivalent.

**Fix:** put the four limits in `protocol.py` beside `MAX_FRAME`, check them in
`encode()`, and have `decode()` refuse them too.

---

## 06.5 — LOW — header lookup is case-insensitive on the guest and
case-sensitive everywhere else

`rc_frame_get` (`frameproto.cpp:57-64`) matches with `_stricmp`. The duplicate
check twelve lines below it (`frameproto.cpp:143-147`) uses `strcmp`, and the
bridge's `headers` is a plain dict.

So `id=a` plus `ID=b` in one frame: the bridge's duplicate check passes (two
distinct keys), the guest's duplicate check passes (case-sensitive), and
`rc_frame_get("id")` returns whichever came first. Three components, three
different answers about whether those are the same header.

Not reachable today -- the bridge builds headers from literals -- so this is a
latent inconsistency rather than a bug. Worth a line because the guest's parser
is explicitly written to be correct *"for the case where the bridge is what might
be wrong"*, and here it is more permissive than the thing it is checking.

**Fix:** `strcmp` in `rc_frame_get`, matching the duplicate check beside it.

---

## 06.6 — LOW — a client rule that is not declared at handshake

`CLAUDE.md`: *"Client rules are declared at handshake, never assumed."* The guest
declares fifteen (`acp`, `case`, `eol`, `illegal`, `maxpath`, `maxcomp`,
`maxdepth`, `dirreserve`, `workreserve`, `shortnames`, `trailing`, `devices`,
`manifest`, `oemcp`, `max_frame`) and `caps.py` refuses an unknown value for each.
That machinery is good and the commit that closed the parsed-but-never-sent gap
(`e389e8f`) did close it -- I diffed the two lists and they agree.

One limit escaped it. `RC_SYNC_MAX_FILES` (`win/sync/sync.h:22`, 2000) is not
declared, and the bridge hardcodes the same number as `SYNC_MAX_FILES`
(`bridge/files/syncrun.py:51`) with a comment calling itself a *"prose
restatement of the guest's own RC_SYNC_MAX_FILES"*. `SYNCEND` returns
`status="too_many"` with no number, so `syncrun.py:187` quotes a figure it
assumed rather than one it was told.

`maxdepth` -- the same kind of limit, from the same header file, two lines
away -- **is** negotiated. There is no principle separating them.

The failure is mild: change `RC_SYNC_MAX_FILES` and the refusal message cites the
wrong number. But it is the exact shape the handshake exists to prevent, and it
costs one `HDR("maxfiles", ...)` and one `_num` row to close.

**Also:** `caps.py:341-344`'s `declared` tuple omits `maxdepth`, though
`from_hello` parses it three lines later. A client declaring only `maxdepth`
is logged as *"assumed (client did not say)"*. One word fix.

---

## 06.7 — LOW today, MEDIUM at Phase 5/6 — the guest has no compressor, so
guest→bridge is uncompressed whatever the codec table says

`docs/BACKLOG.md` §8 owns the question of *which codec values exist*
(`rle8`/`rle8+deflate`, named in `PLAN.md` and never built) and concludes,
reasonably, that nothing shipping today needs them. One adjacent fact is not
covered by it.

**The guest cannot compress.** `win/net/net.cpp` links `mz_inflateInit`,
`mz_inflate` and `mz_inflateEnd` and nothing else -- there is no `mz_deflate`
anywhere in `win/`, and `"enc"` appears exactly once in the guest, in the
*reader* (`net.cpp:191`). The guest never sets `enc=` on anything it sends, so
**guest→bridge is uncompressed in every direction the codec table could
describe**, and `compress.py:31`'s `LEVEL_UP = 1` -- the level chosen for the
P200 -- has no reference anywhere in the tree. §8 reads as a question about enum
values; the outbound half is a missing compressor, which is a larger piece of
work and belongs in the Phase 5/6 estimate rather than in a codec decision.
`PLAN.md` R3 calls guest→bridge *"the only direction needing care"*.

This costs little today -- uploads are manifests, `SYNCDAT` bodies and exec
output -- but it is exactly the Phase 5 (attachments) and Phase 6 (screenshots)
path, which `PLAN.md` sizes at 15-24 s per screenshot on dialup *with*
compression.

**`BACKLOG.md` §8's stated justification is factually wrong**, which matters
because it is what the "intentional-so-far" reading rests on. It asserts that
`deflate` alone *"covers everything that ships today (transcript text, sync file
bodies)"*. Sync file bodies are **never** compressed in either direction:
`bridge/files/synctransport.py:145-150` sends `SYNCPUT` bodies raw, and `enc=`
is set only on `TEXT` and `EXPORTDAT`. So §8 is deferring on the strength of a
claim about current coverage that does not hold. (Independently reached as
`03.4` in the requirements report.)

**Correction to an earlier draft of this finding.** I first wrote that `STAT`
did not exist and that `link_bps` was a command-line flag, making the adaptive
design unbuilt and `should_compress`'s docstring false. **That was wrong**, from
a grep confined to `protocol.py` and `net.cpp`. `STAT` is built and wired end to
end: the guest samples its own throughput and sends it (`win/rpc/rpc.cpp:205-220`,
only when bytes actually moved, so an idle link does not pin the bridge to its
slowest codec), the bridge dispatches it (`server.py:2005`) and `_on_stat`
(`server.py:1799-1809`) feeds `set_link_speed`. The docstring is accurate and
the adaptive path works. `rpc.cpp:196` records that this was itself a recent
fix -- the handler existed from the beginning and the guest had never sent one,
so every threshold was inert. What the thresholds are *worth* is a separate
question, and `03.3` in the requirements report raises it: the 400 kB/s cut-off
rests on a Phase 0 measurement `PROGRESS.md:52` marks *"provisional, not usable
as a threshold"*.

---

## What is right, and worth not breaking

- **The framing has no resync and both sides say so.** `FrameReader.feed` raises
  and `ProtocolError` is documented as always fatal; `net.cpp:159` breaks the
  loop. Neither side attempts recovery on a stream whose framing is lost, which
  is the correct answer and an unusual one to actually hold to.
- **The body-versus-header discipline is sound where it was applied.** `EXEC`'s
  command, `HALT`'s text and `turn_error`'s message are all in bodies, each with
  the incident that moved them recorded in the docstring. 06.1 is that discipline
  being incomplete, not absent.
- **`exec_cmd` uses `fold.encode_exact` and raises rather than transliterating.**
  A command line that cannot survive the codepage is refused instead of
  approximated. That distinction -- display text folds, command text does not --
  is correct and easy to have got wrong.
- **The lane table is a table.** `RC_LANE_TABLE` plus one documented `SYNC*`
  wildcard, defaulting to `RC_LANE_UI`, with the reasoning for the default
  written down. Phase 6 adds rows, not branches.
- **The send lock is never held across a multi-frame transfer**, which
  `PLAN.md` calls "the one thing that must be right". Verified on both sides:
  `link.py:91` takes the lock inside `send()` for one frame, and `send_html`
  /`send_export` call it per chunk with a `yield_()` between -- one whose
  docstring records that the obvious version was *measured* and found wrong,
  because a chunk loop over an uncontended lock with `drain()` below the
  high-water mark never actually suspends. On the guest, `rc_net_send` holds
  `send_lock` for one frame (`net.cpp:665`) and explicitly drops it around the
  queue-full wait (`net.cpp:711-714`).
- **`caps.py` refuses unknown enum values rather than falling back.** The comment
  gives the reason -- the permissive guess is the wrong one -- and `EOL_CR` is
  refused at handshake specifically because the alternative is a per-file CRC
  mismatch forever.
