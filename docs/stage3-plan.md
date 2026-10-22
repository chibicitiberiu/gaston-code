# Stage 3 — the markup moves to the client

## STATUS, 2026-10-12: COMPLETE — twenty-seven commits, `1c49270..`

`docs/PROGRESS.md` has the outcome and the numbers; `ROADMAP.md` stage 3 has
the summary. The plan below is what was intended, kept as written so the two
can be compared.

**Where it was wrong, and the corrections are in the commits rather than
here:** §3's layer 3 was written and not built -- `boot_local` reported a
missing `view.css` to the log and nowhere else, and `set_document` returned
void, so the two failures the layer exists for could not be reported at all;
and both of §3's status-bar warnings were written against `set_override`,
which is the bridge's turn-status channel and is read only while a turn is
running, at a point in `createChildren` where the status bar does not yet
exist. Neither appeared on the guest. Fixed after the stage was declared
closed, which is itself the finding: the four detection layers were tested
under Wine, where there is no status bar to look at. A3 cost 1,024 bytes and
not ~200; the payload sanitiser cannot live
in the encoder, because by then the payload is full of the marks it would
strip; A1's strip could not come out at A5, because three producers still
hand-built markup; `test_html.py`'s 42 tests were NOT split four ways,
because B2 compares the two renderers to each other and only those
assertions pin the oracle to what MSHTML was given before; and the plan's
instruction to re-point `doc_fingerprint` at `view.css` was a no-op, since
the composed document contains the CSS already.

**Two things it got right that mattered most:** the equality proof found two
real bugs before it was a test, and the guest measurement for the template
round trip was not optional. Neither would have been done without the plan
saying so.

Nineteen commits in three phases were planned; twenty-seven landed.
A11 folded into C2 as the plan expected, and five of the planned commits
needed a sibling the plan had not foreseen. `ROADMAP.md` stage 3. Planned 2026-10-12 and
reviewed three times before any code: a design pass that argued four of the
decisions below back to the user, and two adversarial passes that found 104
defects between them. Four of those killed a design choice outright -- the flat
inline encoding, the `fold`-side sanitiser, the paginator's deleted split arm,
and the cache's `FORMAT`-file-and-discard -- and nine would have left a commit
red. The third pass was aimed at the first pass's own fixes and found two of
them wrong, which is why §2's escaping table and §7's deleted risk read as they
do.

Where an earlier draft of this plan was wrong, the text says so. That is
deliberate: a correction with its reason attached is the cheapest documentation
in the tree, and two of the four killed choices were killed by re-reading a
claim that had already been "fixed" once.


## Context

`docs/ROADMAP.md:204-234` is this stage. Its exit condition, from the table at `:35`, is one
sentence: **"a second client could be written without touching the bridge."** Today that is
false in three ways, and `BACKLOG.md` §13 (`:1397`) and §31 (`:2790`) name two:

- **The markup.** `HtmlStream` (`bridge/view/html.py:228`) returns finished HTML written to
  IE5's constraints and the guest hands the bytes to MSHTML. A terminal client wants runs and a
  width; a DOS client wants lines; both would have to parse HTML to recover structure the bridge
  had already worked out and discarded.
- **The decoration.** `STYLESHEET` (`:445-607`) ships from the far side of the wire, so a guest
  cannot change how the guest looks by rebuilding the guest — which `win/app/htmlview.h:55-58`
  argues by accident when it makes the status slot optional *because* "the bridge's DOC is
  authoritative and may be an older build than this guest".
- **Three numbers calibrated to MSHTML rather than to text**: the 90-block page cap
  (`bridge/view/pages.py:39`), the 56-column tool-output wrap (`html.py:152`) and the
  1,200-character tool-result truncation (`:114`). A client with no layout engine inherits all
  three and can use none. That is also a breach of `CLAUDE.md`'s *the client declares OS facts;
  the bridge holds none*.

Stage 2 paid for this: `gaston.exe` is **478,249 of 482,345** — 4,096 bytes of slack — and
`win/Makefile:146-151` now asks a feature that needs a page to say so with a number.

**Decided in stage 1** (`ROADMAP.md:134-149`), not reopened: export is rendered by the guest;
the vocabulary is declared at the handshake as one versioned key; the cache keeps **rendered**
markup with a format bump; R7 is a stage of its own.

**Decided 2026-10-12:**

1. **The appearance becomes files beside the exe** — CSS *and* markup (§3). Not compiled in.
2. **The block record lives in the frame body**, LF-framed; frame headers do not change.
3. **The cap moves once, measured**, in `win/Makefile`'s ladder in its own idiom.
4. **All three MSHTML numbers become the client's**, and further: *"Server only has `offset` and
   `limit` style parameters for getting the blocks."* The wrap moves **into the renderer** rather
   than being declared, so nothing about appearance is left on the far side; the tool-result cap
   stays declared, because truncation is bandwidth rather than appearance.
5. **Pagination is a stage of its own, after this one** — so this stage keeps the bridge's
   emitter as a verified **oracle**, which is what makes the renderer provable. R7 → 3c.
6. **There is one client.** No `markup=0` fallback, no second bridge renderer.
7. **The markup is a template file, from the renderer's first commit** (§3a), and a missing or
   malformed one **degrades** rather than refusing, because the box is updated by hand.

**Three adversarial review rounds have found 104 defects in this plan**, four of which killed
design choices and nine of which left a commit red. The corrections are in the text, and where
an earlier draft was wrong it is named — that is the cheapest documentation available, and
round 3 found that two of round 1's fixes had been applied wrongly, which is the failure mode
worth guarding hardest.

---

## 1. Why not raw markdown on the wire

Asked twice. Semantic blocks, yes — and the vocabulary uses the semantic word, never the tag:
`code`, not `pre`. Markdown, no, and the reason is this codebase, not the rule:

1. **It is mutually exclusive with decision 4.** A block list you can ask for `from=120 count=40`
   needs something that knows where block 120 begins, and only the parser knows
   (`html.py:286`'s `_line`, `:267`/`:278`'s `_start`/`_close`). Markdown deltas mean the bridge
   cannot index, range-serve, anchor a user turn, or cap a block.
2. **The parse is the larger part.** ~180 lines (grammar `:45-63`, `feed`→`_tail` `:239-399`)
   against ~81 of emit (`_render` `:401-432`, `_table` `:205-225`, `inline` `:75-95`, `esc`
   `:66-72`). Moving the emit moves a switch over a table; moving the parse moves cross-feed
   fence state, the underscore lookarounds C has no lookbehind for, the code-inside-bold
   recursion, and `LIVE_CAP`'s bandwidth policy into C on the exe with 4,096 bytes of slack.
3. **Half the transcript is not markdown.** Tool calls, tool results, the permission card,
   notices, errors, the user echo and the thinking stream are records. Markdown needs a second
   mechanism for all of them — two vocabularies where one will do.
4. **There is no pass-through anyway.** `fold.encode` (`fold.py:216`) needs a 28-codepage table
   and `unicodedata`; a markdown wire is *folded* markdown, already bridge-processed.

**Markdown stays reachable later as a vocabulary value rather than a reversal** — the bridge
keeps the folded line stream, and `caps.py:379-394`'s shape adds a value without a flag day.
Recorded in §31 as an answer, not a refusal: for a terminal client that already links a
renderer, markdown would be the better wire.

**Where the line falls.** The bridge decides *what a thing is*; the client decides *what it
looks like*. Elements, classes, escaping, entities, line breaks, nesting and document assembly
are all client-side after this; what stays is analysis. **The test is the exit condition:** a
client writing to a teletype consumes this vocabulary without discarding or parsing anything.
`kind=pre` fails that; `kind=code` passes.

**The wrap moves to the client** (`_fold_long_lines`, `html.py:155`), because the client knows
its window width at runtime and 56 was measured in the default 640×480 window (`:146-151`).
**The honest limit:** a resize does not re-wrap blocks already in the document — the client
holds their rendered HTML, not their records, and keeping records would cost heap on a machine
measured at 28 KB free (`PROGRESS.md:720,744`). New blocks and a re-rendered page pick up the
new width. **`_TOOL_RESULT_MAX` stays on the bridge**: truncating on the client means the bytes
already crossed a 5 KB/s link, so it is bandwidth, not appearance. `toolmax=0` means do not.

---

## 2. The wire

**The record lives in the frame body. Frame headers do not change.** `text_frame`
(`protocol.py:356`) keeps `mode`, `enc`, `page`, `total`, `off`; `Turn::onText`
(`win/app/turn.cpp:180`) keeps reading `mode`.

```
<kind> [<attr>=<value>]*      LF    first line, ASCII, bounded to 128 bytes
<tag>[ <payload>]             LF    zero or more; tag is one ASCII letter
.                             LF    end of record
```

Why the body rather than frame headers, which is where this plan started: a 90-block page goes
out as **one** deflated frame today, because `Page.add` (`pages.py:63-70`) coalesces same-style
runs and `style` is always `"html"` (`presentation.py:87`). One frame per block costs ~40 header
bytes each plus independent deflate per frame (`link.py:84-88`) on a 5 KB/s link; export would
become thousands of frames and rewrite `ChunkSink`'s offset protocol; and the composites need a
body parser regardless, so headers would have saved the key=value scan and left the kind encoded
twice.

- **A tag with no payload is a tag letter alone**, which is load-bearing: `user_turn('a\n\nb')`
  is `<div class="you">a<br><br>b</div>` today (`html.py:105-108`), so an empty payload line has
  to be expressible or blank lines in a pasted message vanish.
- **No dot-stuffing.** An earlier draft specified it; it is dead code here, because a payload
  line always begins with its tag letter and only the terminator begins with `.`.
- **Attributes carry bounded enumerations and small integers; all text rides in payload lines.**
  `code`'s language was the one violation — `_FENCE` (`html.py:45`) captures it unbounded — so it
  is an `l` payload line, which needs no clipping and so changes no HTML.
- **`depth` and `start` are not in the vocabulary**: `_BULLET` (`:47`) captures the indent and
  marker and `_line` (`:336-343`) discards both; `_render` emits a flat `<ul>`/`<ol>`. Listing
  them would have had the agreement test bless fields nothing fills.
- **A frame body is zero or more WHOLE records.** Zero is real — `link.py:122-124` sends an empty
  LIVE body to clear `#l`.
- **`pack`'s budget is in POST-FOLD BYTES**, because folding *expands*: `fold.encode('a…b')` is
  `b'a...b'` and `—` becomes `--`. A body packed to budget in characters can exceed it in bytes,
  after which `link.py:146-169` would cut at a byte offset — so `pack` folds as it measures, and
  C2 retires `send_html`'s chunk loop for TEXT.

### The inline marks are nested, not flat

An earlier draft used a flat 3-bit state byte on three claims, **all false**, verified by running
the code:

- `<b><code>x</code></b>` and `<code><b>x</b></code>` do **not** render alike: `code` is
  specified with the `font:` **shorthand** (`html.py:502-503`), which resets `font-weight` and
  `font-style`.
- Nesting reaches **depth three**: `inline("__a *b \`c\` d* e__")` → `<b>a <i>b <code>c</code>
  d</i> e</b>`.
- A flat state is **lossy**: `inline("**a****b**")` → `<b>a</b><b>b</b>`, two spans a flat run
  list cannot distinguish from one.

**So `\x01` plus one letter opens a span — `c` code, `s` strong, `e` emphasis — and `\x02` closes
the innermost.** "Closes innermost" is provably sufficient: `inline` concatenates balanced
fragments, so the output is always well-nested. **And the stack must be a real stack, not a
bitmask or one slot per kind**, because a kind nests inside itself: `inline("_a *b* c_")` →
`<i>a <i>b</i> c</i>`. Depth is structurally bounded at three — `**X**`/`*X*` exclude `*` from X
and `__X__`/`_X_` exclude `_`, so a chain uses at most one `*`-family level, one `_`-family level
and code — so four deep is enough, with the bound checked rather than assumed.

### Escaping: three modes, and the distinction does not collapse

An earlier draft said `esc_into`'s comment at `htmlview.cpp:771-790` — that the bridge's escaper
and the guest's "never see the same string" — stops being true once the guest renders. **It
stays true, and sharpens.** After this stage there are two sources of text and they keep
different policies:

| mode | escapes | newlines | used for |
|---|---|---|---|
| record prose | `&amp;` `<` `>` | `<br>` between `t` lines | every bridge-sent block except `code`/`toolresult` |
| record literal | `&amp;` `<` `>` | kept as `\n` inside `<pre>` | `code`, `toolresult` |
| local text | `&amp;` `<` `>` `"` | `<br>`, `\r` dropped | what the guest composes itself |

**`esc` does not escape `"`** — it is `html.escape(quote=False)` (`:66-72`), and
`user_turn('say "hi"')` emits a raw quote today, verified. `esc_into` escapes four
(`htmlview.cpp:805`). So "the four escapes" was wrong, and a renderer that escaped `"` in record
content would produce `&amp;quot;` where the oracle produces `"` — a double-escape in the one
place none of the five escaping tests looks.

**`\r` is passed through in record content and dropped only in local text.** An earlier draft had
this backwards and cited the wrong function: `feed` splits with `str.splitlines()`
(`html.py:243-247`), which splits on `\r` too, so `a\rb\n` is already `<p>a<br>b</p>` — verified.
A `\r` survives literally only in the unterminated tail, where today's HTML carries it raw; so
the renderer carries it raw, and `esc_into` keeps dropping it for locally-composed strings. That
is what makes A1's test, B1's cases and B2's byte equality consistent, which as specified they
were not.

### The reserved bytes, and a real bug underneath

`\x00` in model text **truncates a block today**: the guest NUL-terminates every body
(`win/wire/connection.cpp:280` deflate, `:689` raw) and `rc_html::block` takes a `const char *`
(`htmlview.h:156`). The asymmetry makes it a silent partial loss: `turn.cpp:190` hands the
**cache** `(f->body, f->body_len)`, so the cache keeps the full bytes and only the DOM truncates.

**The sanitiser belongs in the record encoder, not in `fold`** — an earlier draft put it in
`_fold_char` and it would have been a literal no-op, because `fold()` short-circuits on
`if text.isascii()` (`fold.py:210-211`); verified, `fold.encode('a\x00b\x01c')` returns the bytes
unchanged. `fold.encode` is also not the display path: `protocol.py:48-49` routes every header
value through it. So `blocks.encode` drops `\x00`, `\x01` and `\x02` and nothing else, A1 is the
narrow display fix only, and **A1's strip is removed when A5's lands** so two do not coexist.

### The vocabulary

| kind | attrs | payload | `t` treatment |
|---|---|---|---|
| `para` | — | `t` | prose, `<br>` |
| `code` | `clipped` N | `l` lang, `t` lines | literal |
| `heading` | `level` 1-6 | `t` | prose |
| `bullet` / `numbered` | — | one `t` per item | prose |
| `quote` | — | `t` | prose, `<br>` |
| `rule` | — | — | — |
| `table` | `header` 0/1 | `r` row, `c` cell | prose |
| `user` | — | `t` | prose, `<br>` |
| `notice` / `error` | — | `t` | prose, `\n` |
| `tool` | — | `n` name, `t` summary | prose, `\n` |
| `toolresult` | `error` 0/1, `clipped` N | `t` | literal |
| `thinking` | — | `t` | prose, `\n` |
| `permission` | `settleable` 0/1 | `h` kind, `t` para, `w` warning, `p` preview, `s` step, `k`/`v` row | prose |
| `clip` | `dropped` N | — | — |
| `exportheading` | — | `h` title, `t` when | prose |

**`clipped` is on `code` as well as `toolresult`**, and that is not symmetry for its own sake: the
realistic oversize record is a fenced block, and without the attribute C2 would turn today's
truncated-with-a-marker block into a refused one — i.e. the block would vanish from the
transcript rather than arrive short.

**`permission`'s `h` is a kind, not prose.** An earlier draft made it the head *text*, which
would have put "Permission required" and "Answered" on the wire and broken §2's own rule that no
English furniture crosses it — and could not have served `rules_view`, which sources its head
from `prompts.text("policy.view.head")` (`permissions.py:493`). So `h` names which head, and the
client holds both strings. The bold labels inside the card ("Check this one:", "What it
touches:") are the client's furniture for the same reason, with `w` and `p` carrying only their
text.

**`link` is reserved and not implemented.** `BACKLOG.md:2851` names it, but `_INLINE` has three
groups and there is no `[text](url)` regex, no `href`, no `<a>` rule — verified,
`inline("see [text](http://x/) here")` returns its input. The reason advertises the stage: the
transcript is a `WebBrowser` control, so an `<a href>` navigates it and takes the transcript with
it.

---

## 3. The appearance: three files the client owns

`view.htm` ships beside the exe today, **generated from `bridge/view/html.py` at package time**
(`tools/gen/mktools-iso.sh:167-175`), with an argument at `:154-157`: *"two copies of a
stylesheet drift."* Right while the bridge is authoritative, and it **stops applying the moment
the client is**. The comment block is **replaced, not deleted**: it records a decision.

**Three files shipped, and the client composes the document.** `win/resources/view.css` (today's
`STYLESHEET`, comments and all — they are the only record of why several rules exist and three
are guest measurements), `win/resources/export.css` (was `EXPORT_CSS`), and
`win/resources/view.tpl` (§3a). At boot the client writes `view.htm` beside the exe:
`<meta charset>` from its own `GetACP()`, `view.css` **inlined** in a `<style>`, and
`#t`/`#l`/`#s`. All three are **CRLF-converted when staged**, like every other text file the kit
ships (`sed -i 's/$/\r/'` on `GREMOTE.INI` `:124`, `GREMOTE.REG` `:142`, `SETUP.BAT` `:260`) —
`need` is a bare `cp` (`:26-29`) and an LF file into a Win9x profile parser is not a risk worth
taking. `:250`'s `COPY /Y D:\VIEW.HTM` is **replaced**, not extended, since `VIEW.HTM` stops
being staged; `README.TXT` (`:262+`) enumerates the payload and moves too.

Inlining rather than `<link rel="stylesheet">`: that is **not proven on this platform** — nothing
in the tree uses one, and the two findings that look like precedent (`mktools-iso.sh:163-164`,
`docs/audit/04-planned-features.md:879`) are both about the **image loader** and `data:` URIs.
Inlining also keeps `set_document`'s write-then-navigate shape (`htmlview.cpp:580`) and its
failure paths at `:621-641`, and stops `view.htm` being a shipped artefact.

**The skeleton stays compiled in** — about 200 bytes — because the three slot ids are found at
runtime by `find_id`, so they must be known to C++ anyway, and a template whose keys the code
must also hold as constants is two sources for one fact. The **export** document skeleton stays
compiled with it, for the same reason.

### 3a. The markup is a template file

Asked while planning: *"put the html snippets in template file or files, like a simplified
'jinja' where we just have macros that are replaced in text, which makes design simpler and
easier to change."* Taken, because it closes an asymmetry: otherwise the *style* is an editable
file while the *structure* is in the image, so half the appearance still needs a rebuild —
and `PROGRESS.md:955-958` records what that costs ("a 4-minute iteration into a 30-second one").

**The format stays below a parser by putting control flow in code.** A template holds **at most
one `{}`** — the children hole — and any number of `{name}` value holes, **repeated `{name}`
supported** because `heading` needs `{level}` in both the prefix and the suffix.
`rc_tpl_emit(key, values, sink)` writes the prefix, the caller writes children, then the suffix.
Loops and conditionals stay in C++: the code loops rows and calls `row`/`cell`, and the code
decides whether to emit `lang` or `clip` at all. **A variant is a second key, not a conditional**
— `toolresult` and `toolresult_err`, the two permission heads — with the agreement between a pair
asserted by the vocabulary test, since duplicated wrappers are a drift risk the §13 argument
applies to.

**What the file owns, and what stays compiled.** It owns **every block wrapper and its class
names** — which is where design changes actually happen. Three things stay compiled, and saying
so is what keeps the claim honest rather than discovering it later:

- **The `<br>` joiner and the three inline marks** (`<code>`, `<b>`, `<i>`). A separator is
  neither the children hole nor a value hole, and an inline mark is one token a designer never
  wants to restyle in markup — they restyle it in `view.css`, which is the file for that.
- **`tool` is one key with two holes**, not `tool` + `toolname`: the space after `</span>`
  (`html.py:118-120`) is trailing whitespace in a `toolname` value and
  `GetPrivateProfileStringA` strips it.
- **Two classes are DOM operations, not markup.** `settle_perm` writes `RC_CSS_PERMDONE` and
  `settle_pending` writes `RC_CSS_USER` through `put_className` (`htmlview.cpp:906,928`); five
  more `RC_CSS_*` are passed to `say()` by the guest's own `notice`/`error`/`user`
  (`:934-937,970-973`). So **`RC_CSS_*` does not mostly disappear** — an earlier draft said it
  did, and that `test_guest_css.py` would "parse no C at all". It parses less C and gains one
  contract: `RC_CSS_USER`'s `"you"` must appear in the `user` template's class list, which the
  re-pointed test asserts.

**Read with `IniFile` (`win/core/inifile.h`), which is already linked — but it cannot report two
of the three failures on its own, and an earlier draft assumed it could.** Verified against
`inifile.cpp`:

- **A missing file is invisible.** `ok()` (`:24-26`) only checks the path has a separator; a
  missing `view.tpl` yields silent `getString` successes with the caller's defaults, no
  `error()`, no key to name. So the loader does its own `GetFileAttributesA` first.
- **An emptied value is a success.** `para=` reads back present-and-empty
  (`inifilecheck.cpp:46,170-174`), so one stray edit would render every paragraph as nothing with
  no log line. `IniFile`'s "present but wrong is an error" is its contract for *numbers and
  over-long strings*. So the loader requires every known key **present and non-empty**.
- **A mistyped `[marku]` passes `checkKeys`**, which enumerates the section's keys and finds none
  (`:233-263`). The all-keys-present pass closes that too.
- `checkKeys` *does* catch the typo direction — and because it fails the **file**, one unknown key
  degrades the whole appearance rather than one block. Worth stating plainly rather than
  implying, as an earlier draft did, that a typo costs one block.
- `version` is itself a key and must be in the known list, or the shipped file fails `checkKeys`
  on every boot.

Three behaviours checked and safe: `;` inside a value survives (`inifile.h:16-19` is not
Wine-only — `inifilecheck.cpp:47,225-229` pins it), `=` inside a value is fine (the API splits at
the first one), and quote-stripping never bites, because every value begins with `<`. The
per-value limit is `cap - 1` for whatever `cap` the loader passes (`inifile.cpp:130-139`), so
**`cap` is a stated 256**; `KEY_LIST`'s 2,048 (`:18`) holds names only and 40 names are ~350
bytes.

**It must be measured on the guest, and this is the one scheduling consequence.**
`inifile.cpp:50-107` is 58 lines about this exact API: *"GetPrivateProfileStringA on Windows 98
will neither hand a control-character default BACK nor READ one out of a file… WINE HANDS IT BACK
INTACT, which is why every test passed on three machines and the guest fell over on the first
one"*, and then *"'Wine proves the code, never the platform', and this is what that rule costs
when the thing being proved is a round trip through somebody else's parser."* A template file is
exactly that round trip, carrying `"`, `;`, `&amp;`, `=`, `<`, `>` and significant internal
spaces, and `inifilecheck`'s longest string case is seven bytes (`:160-174`). So B3 does not
assert platform behaviour from the header: it ships a `rendercheck --dump-templates` mode and
compares the loaded values byte-for-byte against the repo file **on the VM and on the Athlon**
before B2's equality proof is trusted. `research/measurements/inifile-absent-key-p200-20261001.txt`
is the precedent for what skipping this costs.

**Where they live at runtime, which an earlier draft left unstated — and all three obvious shapes
fail.** `static Buf<256> g_tpl[40]` has a non-trivial constructor and fails the
static-constructor gate; a 40×256 POD table is **10 KB of BSS**, more than twice stage 2's whole
slack, against `tooldet.cpp:214-220`'s measured "2KB of BSS on a machine with 28KB free is a real
cost"; a local table during load exceeds `stackframes.py`'s 4,096. So: **one heap buffer plus one
offset per key**, the `core/textlist.h` shape, written for exactly this reason, with the file
refused past a stated cap. Re-reading per emit is not an option — `has()` makes two profile calls
and `getString` a third (`inifile.cpp:113-114,136`) on an API that opens and closes the file each
time, inside the path §8 budgets at 5 ms per 4 KB.

**The substituter scans byte-by-byte and never hands a loaded value to `rc_strf`.** `sprintf` is
banned and `rc_strf` is the only formatter, and `statusbar.cpp:148` is today's precedent for
building this markup *with a format string* — so a designer writing `width:100%` into `view.tpl`
would otherwise be an arbitrary format directive against a mismatched argument list.

**The size claim was wrong in both directions.** The guest's *entire* markup-literal inventory
today is ~210 bytes (`say_id`'s two formats `:882,885`, `statusbar.cpp:149-151`, `esc_into`'s five
replacements, the `RC_CSS_*`/`RC_ID_*` strings); the ~800 an earlier draft credited is
**bridge-side** bytes the image has never held. And ~500 cannot cover a 40-entry key table whose
names alone are ~350 bytes plus 160 of pointers. So this is a **net cost of roughly 700-1,000
bytes**, it belongs in C0's attribution, and the honest framing is that it buys iteration, not
space.

**The example, with its real text**, since an earlier draft's `clip` dropped today's prefix and
suffix and so contradicted the one claim §3a leans on:

```ini
[markup]
version    = 1
para       = <p>{}</p>
heading    = <h{level}>{}</h{level}>
code       = <div class="codewrap">{}</div>
lang       = <div class="lang">{text}</div>
pre        = <pre>{}</pre>
table      = <table>{}</table>
row        = <tr>{}</tr>
cell       = <td>{}</td>
headcell   = <th>{}</th>
tool       = <div class="tool"><span class="toolname">{name}</span> {text}</div>
toolresult = <div class="toolresult"><pre>{}</pre></div>
clip       = <div class="clip">-- shown to you in full; {dropped} more characters were sent to the model --</div>
status     = <div class="status"><img src="spin.gif" width="16" height="16" alt="">{text}</div>
```

Counted through, every kind needs one to ten keys and the total is **~40**: the fourteen above
plus `toolresult_err`, `quote`, `rule`, `bullet`/`numbered`/`item`, `user`, `pending`, `notice`,
`error`, `thinking`, ten `perm*`, and `exporthead` with its two. An earlier draft said 34 and
listed 36 while omitting `pending`, `headcell` and five others.

**And it does not weaken the equality proof.** B2 compares the guest against the bridge's
hand-written emitter, so the template is the thing *under test* rather than a second
implementation — no Python twin, no drift — and the shipped `view.tpl` is therefore **proven
byte-identical to today's markup**. An edited copy is the editor's business, as an edited
`view.css` is. One asymmetry to record: C5 turns B2 into a checked-in fixture, which pins
`view.tpl` byte-for-byte in the repo in a way `view.css` is not.

### The four detection layers

1. **At commit time.** `test_guest_css.py` re-pointed at `view.tpl`, `view.css` and the five
   `RC_CSS_*` that remain C-side: every class either side names has a rule, with the count
   asserted, and `RC_CSS_USER` appears in the `user` template's class list.
   `test_block_vocab.py` adds the other half: every kind has the template keys it needs, each
   variant pair agrees, and the key set matches `checkKeys`'s known list.
2. **A missing or malformed `view.tpl` degrades.** `IniFile` would refuse to start, which is
   right for a settings file and wrong for a machine updated by a manual `COPY` — so a
   compiled-in last resort renders everything as escaped text in a `<p>`. **Not ~80 bytes**, as
   an earlier draft guessed: with the log format string, the notice and layer 4's message, the
   literals alone exceed that, so budget ~250-400. **And the warning does not go only to the
   transcript**, because in the degenerate state a transcript notice renders as an unstyled `<p>`
   indistinguishable from model prose, in a document whose CSS is probably also missing — all
   three files ship and `COPY` together. It goes to the status bar, which is pane text rather
   than HTML, and to the log.
3. **No writable program directory is still fatal, and the hook is `boot_local`.** With no
   `view.css` the client writes an unstyled document; with nowhere to write, `boot_local()`
   returns 0 at `htmlview.cpp:473-476` **without calling `set_document`**, so `CoCreateInstance`
   never runs and `poll()` returns at `:727` long before the containers check at `:745-748`. The
   ~15 lines go on that early return: a status line and a message box naming the directory.
   **`boot_local` therefore does not fold away** — an earlier draft had B4 delete it while this
   layer depended on it, and it has an external caller at `mainwindow.cpp:1001`.
4. **Staleness.** The client compares `view.css`'s and `view.tpl`'s version against
   `RC_VIEW_VERSION`; a mismatch is a status line and a log line, loud and non-fatal, because a
   stale appearance beats no transcript.

**One property is genuinely lost.** Today a CSS edit lands on a *reconnect*, because
`set_document` fingerprints the incoming DOC (`htmlview.cpp:534-541,597-609`). After C4 there is
no DOC, so an edit lands on the next **start**. The right trade now the files are the client's
own, but a real regression against `PROGRESS.md:955-958`, and recorded as one.

---

## 4. Commits

Three phases. **Everything bridge-side lands first with no wire change, the renderer is proved
under Wine before it is linked, and the cutover is one commit because it cannot be two.** The
ordering rule round 3 forced, after it found nine red commits: **a deletion lands with its last
caller, and a test re-point lands with the change that breaks it** — never earlier, never later.

### Phase A — bridge-side, nothing on the wire changes

0. **A0 — correct what the stage was scheduled against.** `docs/stage3-plan.md` from this file,
   and §6's corrections applied where they live. No code. Stage 2 opened the same way and it was
   worth it.
1. **A1 — `\x00` stops truncating a block.** The narrow display fix at `link.py:121` plus tests
   with a literal `\x00`, and one asserting `\r` and `\t` still travel. Not in `fold` (§2), and
   not a blanket C0 strip. **A5 removes this strip when it adds the encoder's.**
2. **A2 — the probes' `enc` argument.** `viewprobe.py:102` **and `:107`**, and `barprobe.py:103`,
   pass `"plain"` where `text_frame` wants `raw`/`deflate`. These tools are the stage's visual
   instrument and are in no automated gate, which is why they are touched early and by name.
3. **A3 — live-cost instrumentation and the BEFORE number.** Timing around the render step and
   `put_innerHTML` in `rc_html::live` (`htmlview.cpp:1018`). QPC for the reason
   `connection.cpp:664-667` records, **guarded like every other QPC site in this tree**
   (`:298-313`, `:669-671`) — a bare call would be the only unguarded one — and **the fallback
   refuses rather than degrades**, because §8's threshold is 5 ms and a 55 ms tick cannot resolve
   it (`PROGRESS.md:87`: *"Do not budget against them"*). `viewprobe --replay-fence` streams a
   canned 60 KB fence at the real 100 ms cadence. **First commit that moves the image**, built as
   a permanent facility because stage 5 wants the same measurement.
   **MEASURED: it cost 1,024 bytes committed, not the ~200 estimated** — 672 of code and 192 of
   literals, rounded up by section alignment — taking the slack from 4,096 to 3,072, which is
   C0's to carry. The estimate counted the two log format strings and not the 64-bit counter
   arithmetic at each site; `live_report` alone was 210 bytes until its computed means were
   dropped, on the argument that totals and a count are the data and a mean is analysis the
   reader can do for free.
   **And `--replay-fence` has to push through `GuestLink.send_html`, not bare `text_frame`:** the
   sealing APPEND of a 60 KB fence is 65,821 bytes on the wire against a 65,536 cap, so a probe
   that frames its own sends crashes on the last one — found by running it. Going through the real
   path also means the measurement sees production's deflate decision and clip marker instead of
   a second copy of that policy.
4. **A4 — split `html.py`, behaviour-identical.** `blocks.py` gets `Block`, the grammar,
   `BlockStream` and `KINDS`/`ATTRS`/`VERSION`; `htmlrender.py` gets `render(Block) -> str`;
   `html.py` becomes a shim.
   **Not a mechanical move, three ways.** `hr` and `heading` never go through `_render` — they
   are emitted inline at `:315` and `:323` and `_render` has no branch for either — so those
   Blocks are built on the `_line` side, with `min(len(hashes)+2, 6)` (`:322`) preserved on both
   sides for `test_html.py:80`. The shim must **re-export everything anyone imports**, which is
   more than the test file: `notice`, `error`, `tool_call`, `tool_result`, `user_turn`, `esc`,
   `inline`, `document`, `export_document`, `STYLESHEET`, `WIRE_CSS`, `EXPORT_CSS`, `Update`,
   `APPEND`, `LIVE`, `HtmlStream.LIVE_CAP` (`html.py:368`), `_without_comments` (`:626`),
   `_fold_long_lines`, `_TOOL_WRAP_COL` (`test_html.py:259,275`) and `_TOOL_RESULT_MAX` — for
   importers in `bridge/server.py:47-48`, `bridge/services/{sync,turn_runner,session,options,
   workspace}.py`, `bridge/view/transcript.py:29`, `bridge/wire/link.py:23`,
   `tests/python/agent/test_sessions.py:81` and `tests/python/wire/test_fold.py:255`. And it says
   where `_fold_long_lines`/`_TOOL_RESULT_MAX` land, because A12 and B1 both assume it.
   *Proves:* **all 42 tests in `test_html.py` pass unchanged**, which is the point —
   `htmlrender.py` is the oracle for the rest of the stage.
5. **A5 — the record codec, bridge-side only.** `encode`, `pack(records, budget_bytes)` which
   folds as it measures and never splits a record, `decode` for tests, and the payload sanitiser.
   **Two corrections, both found by writing it.**
   **The sanitiser cannot live in `blocks.encode`.** By the time a payload reaches the encoder it
   is full of `\x01` and `\x02`, so a strip there would remove the marks themselves and the
   record would arrive as one unstyled run. It has to be upstream of the marks — `clean()`, at
   the moment model text becomes payload — which also means `Block.payload` is clean by
   construction and the encoder has nothing to sanitise.
   **And A1's strip does NOT come out here.** It can only go once no caller can hand `send_html`
   unclean text, and three producers still hand-build markup at A5: `permissions.py` (A8),
   `sessions.py` (A9) and `presentation.py`'s thinking path (A10). It comes out at **C2**, when
   `send_html` stops carrying TEXT at all.
   **The inline marks land here too**, because the record shape is first realised here: the parse
   emits `\x01`/`\x02` into the payload and `htmlrender` grows a `spans()` that consumes them.
   Doing it any later would mean B2 comparing two different inputs — a record with marks against
   a Block with raw markdown — instead of the same record on both sides.
   *Proves:* round-trip properties over every kind, every nesting the grammar produces (depth
   three, same-kind nesting, adjacent same-state runs), an empty payload, a tag-only line, a
   record over budget refused, and `pack` never emitting a body that ends mid-record.
6. **A6 — `write_block` accepts a `Block`** and renders it through `htmlrender`. Nothing on the
   wire changes.
   **There are three TEXT producers, not one**: `presentation.py:70` (LIVE, which takes `u.html`
   and **bypasses `write_block`**), `:92` (APPEND), and `:135` (`send_page`). All three take
   `str | Block`, and `bridge/wire/link.py` gains the renderer import because `Update.html`
   becoming a union otherwise hands one to `send_html(html: str)`.
   **`make lint` runs mypy as well as ruff** (`Makefile:123-127`) — but `[tool.mypy] files =
   ["bridge", "gremote"]`, so **it never sees `tests/` or `tools/`**. An earlier draft called
   mypy "the instrument that finds every site A7-A11 miss"; it finds the bridge ones.
7. **A7-A11 — convert the producers, one commit each, goldens unchanged.**
   - **A7** the constructors keep their names and return `Block`, so **all 33 `write_block(...)`
     call sites stay** (13 `session.py`, 6 `options.py`, 3 `workspace.py`, 3 `permdesk.py`, 2
     each `permission.py`/`turn_runner.py`/`sync.py`, 2 inside `PresentationService`). **Nine
     consumers do string operations on those return values and break** — and "goldens unchanged"
     covers none of them: `test_html.py:256-258,272-274,282-284` (`out.index("<pre>")`, `in out`,
     `H._TOOL_WRAP_COL`), `test_server_integration.py:2172,2189-2190,2197-2198`,
     `test_sdk_events.py:54-58`, `test_guest_css.py:54-56` (which become assertions against
     `htmlrender.render`), `test_sessions.py:83,90`, `viewprobe.py:91-110`,
     `barprobe.py:104-105`, and `sessions.py:241,253,263`, where a `-> list[str]` would become
     mixed.
   - **A8** `permissions.py:153-209` becomes `Block(kind="permission")` and `:492-509` the same
     kind with `settleable=0`, its four trailing paragraphs as `t` lines, and `h` naming which
     head. **`MAX_BODY` (`:45`, enforced at `:194,200,210`) becomes a record-byte budget**, since
     a `Block` has no rendered length and the bridge no longer computes one; its hand-written
     over-budget fallback at `:203-209` becomes a short record. All 19 `H.esc` calls go.
     `test_permissions_gaston.py` moves here, not at C2.
   - **A9** `sessions.py:265-268`'s hand-built markup becomes `H.code_block`/`H.para`, and
     `:254,270`'s `u.html` becomes `u.block` with `history_html`'s signature changing.
   - **A10** `presentation.py:38-41` becomes `H.thinking(text)` — see §6.
   - **A11 is folded into C2**, because at A11 the wire still carries HTML so there is no record
     serialiser to move the clip cap into, and turning `link.py:146-154` into refuse-and-log is a
     wire change inside a phase declared to have none.
8. **A12 — parameterise `toolmax`**; `Paginator.max_blocks` already is a parameter.
   `_fold_long_lines` is **not** parameterised — it is deleted in C2 and its job lands in B1.

### Phase B — the renderer is proved before it is linked

`win/view/` is invisible to `check-gaston` until C2 adds `$(LIBVIEW)` to `OBJS`
(`win/Makefile:244-245`, before `$(LIBCORE)`), because `CHECK_OWNED = $(OBJS)` derives from
`CLIENT_SRCS` — so the renderer's growth arrives in one measured step. **But the image does move
in this phase**: A3 moved it, and B3 and B4 edit `app/htmlview.cpp`. Each commit records its
`check-gaston` number.

**The premise that governs this phase, and round 3 caught an earlier draft applying it to one
file and not the others:** nothing in `app/` can call into `libview` before C2. So **everything
in `app/` stays as it is through phase B** — the `RC_CSS_*` macros, `say_id`'s escaper,
`settle_*`, `statusbar.cpp` — and the class move, the escaper rewrite and the
`test_guest_css.py` re-point all land in **C2**. An earlier draft put them in B1, which would
have been red for two commits, and re-pointed the test at a file B3 had not yet created.

9. **B1 — `win/view/`, a library, template-driven from its first commit.** `render.h`: the
   `RC_BLK_*` kinds, `RC_ATTR_*`, the versions, `RC_ID_PERM`, and
   `rc_render(const char *rec, unsigned len, unsigned wrapcol, ByteSink &out)` — `wrapcol` as a
   parameter because the view passes its window's column, export a fixed one, and `rendercheck`
   56. **One translation unit**: the substituter sits beside the record parser, because `-xs`
   costs ~2,144 bytes per TU and `PROGRESS.md:4012-4018` is the precedent for absorbing a file.
   `win/resources/view.tpl` lands here, as a repo resource the check exe reads; the CSS files
   land in B3. The record parser uses the cursor-by-reference idiom of
   `win/automation/select.cpp:13-53`, with a function-pointer case table keyed by kind — which is
   also the fix for Watcom inlining a once-called static (`fscheck.cpp:1053-1058`'s 13,268-byte
   frame) and is POD, so it clears the static-constructor gate.
   **The Makefile work:** a `VIEW_SRCS` list, `$(LIBDIR)/libview.lib` and a `wlib` rule beside
   the five at `:756-774`; `rendercheck.exe` with `map=$(BUILD)/rendercheck.map`, because
   `check-%` hands that path to `cmovcheck.py` (`:971-972`) and `stackframes.py` (`:986-987`);
   `check-rendercheck` with **`CHECK_OWNED = $(RENDERCHECK_OBJS)`** — the **full link list
   including `$(LIBVIEW)` and `$(LIBCORE)`**, as every existing target does (`:355`), because map
   modules are named by the `.lib` and never the `.o`: an object list would make `cmovcheck` fail
   outright on a module absent from the map (`cmovcheck.py:131`, `win/Makefile:394-396`) and
   would leave `stackframes.py`'s `$(filter %.lib,...)` empty, silently exempting the renderer via
   `.lib:` in `EXEMPT` — the exact gap `CHECK_OWNED` is there to close. Then `.PHONY` (`:247`),
   `all` (`:563-564`), `check:` (`:837-838`), the root `Makefile`'s `guest-checks`, and the
   `guest` mark on both new suites.
   *Proves:* `rendercheck.exe` under Wine in `fscheck.cpp:1070-1072`'s verdict shape, consumed by
   `tests/python/guest/test_guest_render.py` with `CASE_COUNT` **asserted exactly**. ~45 cases:
   one per kind; **three escape modes** (§2 — three characters in records, `"` left alone, four
   in local text); `\n`→`<br>` in prose and literal inside `<pre>`; `\r` passed through in
   records; every nesting including depth three, same-kind nesting and adjacent same-state runs;
   an unknown kind refused; a truncated record refused; a tag-only line; an empty payload; an
   attribute at and past its bound; a literal NUL and a literal `\x01`; a `ByteSink` returning
   false mid-record; `permission` with each optional field and both heads; the wrap, including
   `wrapcol=0` — **new behaviour with no oracle**, because `_fold_long_lines(text, 0)` **hangs**
   today (verified: `rfind(" ",0,0)` → −1, `cut = 0`, the loop never advances), so the bridge
   helper gains the guard in the same commit; the template layer — `{}` and repeated `{name}`, a
   missing file, an emptied value, a mistyped section, an unknown key, and the degenerate
   fallback with its status-bar warning; and **the same record stream into two sinks
   byte-identical**.
   *Plus* `tests/python/meta/test_block_vocab.py`, in `meta/` because it reads the tree.
10. **B2 — the cross-seam equality proof.** `rendercheck --render` reads records on stdin and
    writes HTML on stdout; `tests/python/guest/test_render_equivalence.py` — in `guest/`, because
    `test_suite_hygiene.py:88-107` requires the `needs_guest` mark for anything touching the Wine
    plumbing — runs ~40 samples (`test_html.py`'s inputs **plus the live-path corpus**, since its
    five LIVE tests at `:292-349` assert on HTML and are not the mechanical parser tests C2's
    split assumes) through `BlockStream` → `encode` → `rendercheck --wrapcol 56 --template
    win/resources/view.tpl` and compares **byte for byte** against `htmlrender.render`. That turns
    `ROADMAP.md:211-212`'s "the same HTML MSHTML gets today" into a fact, makes regenerating the
    goldens safe, and makes the shipped template a proven starting point.
11. **B3 — the CSS files, and the template measured on the guest.** `view.css` and `export.css`
    land with the `.thinking` rule and the version markers; `mktools-iso.sh` loses the generator
    at `:167-175`, gains three CRLF-converted `need` lines, and replaces `:250` and `README.TXT`.
    **And `rendercheck --dump-templates` runs on the VM and the Athlon**, comparing loaded values
    byte-for-byte against the repo file, because §3a's platform risk is one this tree has already
    paid for once. The bridge keeps `STYLESHEET`/`WIRE_CSS` until C4, so there are briefly two
    copies; the guard is a test asserting they are identical modulo comments, which dies with the
    bridge's.
12. **B4 — the document is composed locally.** `set_document` keeps its write-then-navigate shape
    and changes only where the bytes come from: read `view.css`, write `<exedir>\view.htm` with
    the CSS inlined and the charset from `GetACP()`, navigate. `doc_fingerprint` (`:534-541`) is
    re-pointed at `view.css`'s bytes. The failure paths at `:621-641` are **kept**, and
    **`boot_local` is kept** — §3's layer 3 hangs on its early return and `mainwindow.cpp:1001`
    calls it. **B4 does not own the timer**: `TIMER_HTMLPOLL` is armed at `turn.cpp:177` and
    `mainwindow.cpp:1005`, and after C4 the only navigation left is boot, which `:1005` already
    arms — so the arm moves nowhere and C4 confirms it. An earlier draft gave B4 ownership and
    would have had to change `set_document`'s signature inside C4's file to do it.

### C0 — the cap moves, once, measured

The renderer is not in `gaston.exe` until C2, so build C1-C6 locally with `COMMITTED_CAP=999999`
(it is `?=` at `win/Makefile:157`; **never commit the override**), take the number from that
build, attribute with `mapsize.py --module view`, record the shape with `--top 20`, re-read
`cmovcheck.py`. Then **one** commit changing only `:157` and its comment block, in the ladder's
idiom, naming what bought it: ~2,144 bytes of `-xs` for the new TU, the renderer including the
wrap it took from the bridge, the ~700-1,000-byte net cost of the template layer (§3a), and
`render.h`'s literals. C2 then lands against the raised cap and records what the gate prints, so
the ladder's figure and the gate's figure are the same figure.

### Phase C — the cutover

13. **C1 — HELLO gains the client's declarations.** Four keys: `markup`, `blocks`, `toolmax`,
    `maxblock`. No `wrapcol` — the bridge does not know the column because it does not wrap. Four
    `HDR()` sites (**35 → 39** of `RC_HELLO_MAX` 48; 35 is today's unique count by
    `test_hello_headers.py:62`'s regex) and four `RC_CAP_*`; `caps.py` fields with `_num` ranges
    and **`maxblock` clamped against `MAX_FRAME`**.
    **Three things an earlier draft missed:** `limits_for` has **two** production callers —
    `handshake.py:121` and `options.py:129-130` — so bypassing one leaves the first PREFS frame
    overwriting the declared `blocks` with a scaled value; `Caps.declared` is a **single bool**
    (`caps.py:343`, set by `any(...)` over a 21-key tuple at `:427-433`), so per-field presence is
    new work and the four keys must join that tuple; and `test_fakegui_conformance.py:98-128`
    parses the `HDR(` keys and asserts FakeGui declares every one, so
    `bridge/devtools/fakegui.py` moves in this commit.
14. **C2 — the cutover. One commit, because it cannot be two.** The bridge flips
    `write_block`/`write_updates`/`send_page` to "encode, pack, send records", and four things the
    structure forces:
    - **`PageRun` holds a list of records**, and so does `Paginator.write`'s `chunks`
      (`pages.py:167`) — `presentation.py:90` joins those into a string today, which would hand
      `pack` a flat string and put it back to re-parsing.
    - **The mid-run split is deleted, and its replacement named.** `pages.py:212-221` cuts on
      bytes, which with records cuts *between payload lines* and leaves two fragments the guest
      must refuse. But deleting the arm alone makes `Paginator.write` **loop forever**: a record
      larger than `hard_bytes` on a fresh page fails all three remaining arms and `while text:`
      never advances. So the replacement is explicit — **a record too large for a page gets a page
      of its own, whole** — and the client's `maxblock` is what bounds it.
    - **A11 lands here:** the clip marker leaves `link.py:146-154` for the serialiser, which sets
      `clipped=N` on `code` as well as `toolresult`; `link.py` keeps refuse-and-log and its chunk
      loop is retired for TEXT. `test_link.py:152-170` moves with it.
    - **The `app/` side moves here, all of it**: `Turn::onText` renders, `rc_html` gains a
      per-record insert path (§7), the `RC_CSS_*` that are markup move into `view.tpl` while the
      five that are runtime facts stay, `say_id` is rewritten onto the `ByteSink` escaper,
      `statusbar.cpp:146-152` switches to the `status` template, `esc_into`'s comment at
      `:771-790` is **kept and sharpened** rather than deleted — §2's three modes mean its claim
      stays true — and `test_guest_css.py` is re-pointed.
    `H.esc` is **not** deleted here: `export_document` still calls it until C3. What goes here is
    `inline` and `_table`.
    **The cache's format bump is a rename, not a discard.** The obvious shape — a `FORMAT` file
    and `open_session` throwing the directory away — **loses data**: the cold-start-with-no-bridge
    path calls `open_session` then bails at `mainwindow.cpp:620-631` if `page_count()` is zero, so
    the first launch after the upgrade with no bridge would delete the cached conversation before
    the "bridge not reachable" notice at `:626-628`, with nothing to re-fetch from — the exact
    scenario C4's proof exercises. It would also need a delete primitive `rc_cache` lacks. So the
    version goes in the filename (`P%03d.HT2` in `path_for`, `cache.cpp:66-69`): `page_count()`
    finds none, the client re-fetches, nothing is deleted, and old files wait for `BACKLOG.md`
    11a. The two comments that document the old name (`cache.cpp:61-63`, `cache.h:98`) move with
    it.
    *Proves, in this order:* B2 green; every golden regenerated **here** (12 scenarios, 23 TEXT
    frames; `test_frame_goldens.py:17-20` says a diff is a bug unless deliberate, and
    `UPDATE_GOLDENS=1` is the mechanism — `golden_utils.py:23,97-101`); `test_pages.py`,
    `test_presentation.py`, `test_sessions.py`, `test_link.py`; full `make check`; `viewprobe`
    screenshots against A3's baseline.
    **`test_html.py`'s 42 tests split four ways here**, and the arithmetic matters because an
    earlier draft's buckets summed to 39: **29** parser tests stay with their assertions rewritten
    to `Block(kind, attrs, lines)` — mechanical except the five LIVE tests, which need the oracle;
    **5** escaping tests (`:33,39,43,47,53`) become `rendercheck` cases **and** keep a bridge-side
    assertion that the payload carries the raw character, both halves because escaping on both
    sides gives `&amp;amp;`; **3** wrap tests (`:245,269,281`) move with `_fold_long_lines`, which
    is deleted here; **5** die with the CSS at C4 (`:139,144,149,158,167`).
15. **C3 — export.** The bridge sends records (`session.py:159`, `all_text()` → `all_blocks()`,
    the heading as `exportheading`) and the guest renders each into the existing sink.
    **Two real changes, not three** — an earlier draft named a third that is not one:
    `export_end` already reports `sink.received()` (`export.cpp:218,250-251`), which after this
    *is* the written-HTML count. What does break is the completeness guard at `:233-239`:
    `if (total && got != total)` compares `EXPORTSTART`'s record-byte total against HTML bytes,
    shows "finished short and has been discarded" and deletes its own temp — so a `recordBytes`
    counter keeps the guard's job against the number it was written for. **And calling
    `sink.write(sink.received(), ...)` retires `ChunkSink`'s own order check**, which
    `win/fs/xfer.h:46-51` calls out as load-bearing (*"It is CHECKED, not trusted… Two of the
    three implementations this replaces did not check"*) — so that is said out loud, the
    `CHUNK_OUT_OF_ORDER` branch at `export.cpp:190-201` goes with it, and `recordBytes` carries
    the ordering invariant instead.
    `H.esc`, `export_document` and `EXPORT_CSS` are deleted **here**, with their last caller.
    The CSS: `rc_export` reads `view.css` and `export.css` into a `<style>` before the first
    chunk, which needs `GetModuleFileNameA` and two `fopen`s and shifts the sink's offset base;
    it holds ~9.4 KB, so `export.h:3-7`'s "THE GUEST NEVER HOLDS THE DOCUMENT" gains a sentence
    saying what it does hold.
    **§13's "assemble what is cached" (`BACKLOG.md:1453`) cannot work** and the entry is
    corrected: ranges are served on demand and `cache.cpp` holds only what arrived, so a
    cache-assembled export silently omits what the user never paged to — worse than a second
    renderer, because the output is plausible and incomplete.
    *Proves:* a `rendercheck` case that the view and export sinks agree byte for byte; a bridge
    test that no EXPORTDAT splits a record; and an end-to-end export **on the VM, opened in
    IE5**, because a Save dialog is modal.
16. **C4 — DOC dies.** `doc()` (`protocol.py:171`), its **one** send site (`server.py:407`),
    `SKELETON`, `document()`, `WIRE_CSS`, `_without_comments`, `STYLESHEET`, `Turn::onDoc` and its
    three declarations (`mainwindow.cpp:448-451`, `mainwindow.h:123`, `turn.h:53`, the
    `g_dispatch` row), and B3's identity test. **`test_frame_contract.py:94` lists `"DOC"` in its
    `required` set and must be edited here** — §2's claim that it "sees nothing move" was true of
    TEXT and false of DOC, and the line an earlier draft cited for the claim is the line
    containing the literal.
    *Proves:* `test_frame_contract.py` sees both sides move in one commit; a **cold start on the
    VM with no bridge** still shows "configure a host" — which also exercises the one DOC
    dependency that is fine: audit 01.54's reconnect fix is in the **READY** handler
    (`mainwindow.cpp:430-445`), so a reconnect with no DOC still goes READY → `goto_live()` →
    PAGESTART → `clear()`, which empties the slots rather than the document. **Five DOC goldens
    move here** (scenarios 01, 04, 05, 10, 12), which `ROADMAP.md:61` already anticipated.
17. **C5 — the guard that keeps the seam closed.** `test_no_markup_on_the_bridge.py`, and an
    earlier draft's version **could not pass**. It walks `ast` string literals — **excluding
    docstrings explicitly, because a docstring is an `ast.Constant`**, which is how
    `bridge/wire/uirec.py:15-16` would match `<b>` — over the real target set, which is **5 files
    after a literal-only walk**, not the 7 an earlier draft named (the other two,
    `pages.py:203-204` and `prompts.py:62`, are exactly the comment and docstring hits the new
    mechanism drops). It also drops three patterns that can never match a literal: `<ul>`/`<ol>`
    are built as `f"<{tag}>"` (`html.py:422-425`) and `<h1>`-`<h6>` as `f"<h{level}>"` (`:323`).
    **What the test cannot see is stated rather than implied:** an interpolated tag name, a
    `%`-format, and `"<" + "p>"`. Allowlist `htmlrender.py`; then delete it and the allowlist,
    turning B2 into a checked-in fixture. **This is the commit that makes the exit condition a
    test rather than a claim.**
18. **C6 — the docs and the measurements.** `ROADMAP.md` stage 3 done with its numbers, R7
    renumbered 3b → 3c and the pagination stage inserted; §13 and §31 closed carrying §1's answer
    and §6's corrections; `research/measurements/live-*` and the template round-trip;
    `win/Makefile`'s ladder entry.

---

## 5. What the next stage gets

Pagination, with the two exceptions C2 is forced into above. Everything else — `Page`, `_seal`,
`continued`, `MAX_BYTES`, `MAX_LINES`, `HARD_*`, the three `over_*` predicates,
`message_boundary`, `limits_for`, `PAGESEALED`, the four page frames, `pagebar.cpp`'s arithmetic
and the anchors — is next, and nearly all deletion: it exists to divide a stream of HTML into
MSHTML-sized chunks at boundaries safe for MSHTML, and after this stage every boundary is safe
and the client picks the size. `pages.py` is **248 lines** today and ends around 90 as an
append-only `Transcript`.

---

## 6. Corrections the stage carries

Applied in A0, closed with §13/§31 in C6. Each re-verified; several were themselves wrong in an
earlier draft:

- **Two figures that look like the same number are not, and the 21-byte gap between them is
  where every wrong line in the tree comes from.** Measured 2026-10-12: the comments are
  **5,574 bytes in 11 blocks** (largest 1,643) — so `BACKLOG.md:1469` and `PROGRESS.md:4194` are
  exactly right, and an earlier draft of this plan "corrected" them to 5,595, which is the
  mistake §6 exists to catch. But the strip removes **5,595**, because `_without_comments`
  (`html.py:626-628`) also drops the blank lines and trailing whitespace the comments leave.
  So `document()` is **3,962**, and `BACKLOG.md:1472`'s and `stage1-plan.md:137`'s **3,983** is
  9,557 − 5,574: arithmetic on the comment figure instead of a measurement of the result. Both
  lines move to 3,962.
- **The 58%/59% "inconsistency" is two denominators**, and an earlier draft proposed correcting
  the line that is right: 5,574/9,375 = **59.5%** (comments as a share of the stylesheet,
  `PROGRESS.md:4194`'s "59%") and 5,595/9,557 = **58.5%** (the saving as a share of the DOC body,
  `BACKLOG.md:1472`'s "58%"). The single wrong line is `ROADMAP.md:59`, which pairs the 9,375
  stylesheet denominator with the 58% DOC-body percentage.
- `BACKLOG.md:2910`'s **4,919**-byte figure is stale and `BACKLOG.md:1470` already says so by
  name ("not the 8,445/4,919 this entry used to claim"); `:2910` simply missed the
  re-measurement. It becomes 5,574.
- **The strip itself has already landed** — `WIRE_CSS = _without_comments(STYLESHEET)`
  (`html.py:631`), `document()` defaults to it (`:665-666`) — so `ROADMAP.md:58`'s "Needs the
  five DOC goldens regenerated and depends on nothing" is a description of stage 1's intent that
  now reads as an open item. A0 marks it done, so C4's five goldens are not mistaken for it.
- Moved citations: `BACKLOG.md:1411` cites `document()` at `html.py:624-640` (now `:657`);
  `:1443` and `:2889` cite `export_document` at `:643` (now `:668`); so does `ROADMAP.md:105`.
- **`.thinking` is emitted and has never been styled.** `presentation.py:38-41` writes
  `<div class="{style}">` and `session.py:343` passes `style="thinking"`; there is no `.thinking`
  rule and no `RC_CSS_THINKING`. `test_guest_css.py` could not see it because it walks only
  classes the *guest* names — a class the *bridge* invents is invisible. Fixed in A10 and B3.
- **`docs/audit-2/03-requirements.md:411-415`'s `.status` finding is STALE and should be marked
  so.** It says `.status` is written as a bare literal with no `RC_CSS_*`, so the test cannot see
  it; in the current tree `statusbar.cpp:149` uses `RC_CSS_STATUS` and the test does sweep it. An
  earlier draft of this plan cited it twice as live justification for moving that macro. The
  honest reason to move it is uniformity.
- **§31's inline set names a `link` run that does not exist** (`:2851`), and §13's
  cache-assembly answer for export is wrong (C3).
- `BACKLOG.md:521-527`'s standing ask — `send_html` has no END and no status — now sits in the
  new live path, so it is either fixed in C2 or restated with its reason.

---

## 7. Risks

1. **The guest's transient memory.** The figure is **28 KB free after a turn**
   (`PROGRESS.md:720,744`, quoted by `win/Makefile:988`'s gate banner and 20 files in `win/`). An
   earlier draft said 48 KB, which is `ROADMAP.md:217`'s *page-cap* measurement — what a
   200-element page consumed, not what is available. On `block()` the guest holds the inflated
   body (`connection.cpp:271-282`) plus a UTF-16 BSTR (`htmlview.cpp:365-389`) that is 2× the
   *rendered HTML*, not 2× the record; on `say_id` (`:855-891`) it already holds four, so "two
   copies today" was true of one path of three.
   **Mitigation: render and insert per RECORD on the append and replay paths.** **It does not
   apply to `live`:** `rc_html::live` is `put_innerHTML` on `#l`, a wholesale replacement,
   because the bridge re-sends the whole open block every flush (`htmlview.h:51-52`) — inserting
   per record there would append a fresh copy every 100 ms. The live path is already one record.
2. **Double escaping** is the likeliest regression and the least visible. The three modes in §2
   are the design answer; `H.esc` deleted in C3, both-sides assertions in C2's split, and C5's
   grep are the guards. The `"` asymmetry is the specific trap: no existing test covers it.
3. **A7-A11's safety rests on A6 being byte-exact on all three TEXT paths**, hence A6's test on
   each — and on nine consumers mypy cannot see, hence A7 listing them by file and line.
4. **A template file is a round trip through `GetPrivateProfileStringA`**, which this tree has
   already been burned by in the Wine-versus-98 direction (§3a). B3's guest measurement is not
   optional.
5. **Three new files travel by manual `COPY`** (`mktools-iso.sh:250`), so a half-updated box is a
   real state. The staleness marker and the degenerate fallback are what stand between that and a
   mystery report. With no DOC, the kit's warn-and-continue on a failed `VIEW.HTM` generation
   (`:171-174`) would ship a permanently blank transcript — which is why B3 deletes that block and
   uses `need`.
6. **`test_guest_css.py` ends up simpler and, on one half, weaker.** Its
   `test_the_guest_and_the_bridge_agree_on_what_each_class_means` (`:50-56`) exists so "an error
   would render as a notice" is caught; once `view.tpl` names both the class and the markup, both
   sides are one file and that assertion is a tautology. And its fourth test
   (`test_the_container_ids_...`, `:65+`) checks `RC_SLOT_*` against `H.document()`, which C4
   deletes — so it keeps parsing C and its other side becomes the client's own skeleton literal.
   Two of four tests touch `guest_classes()`, not three.

**The build-side traps**, each recorded in the tree: `check-sources` fails on
`sprintf`/`strcpy`/`strcat`/`alloca` and greps `$(CURDIR)`, so `win/view/` is covered
(`win/Makefile:934-951`); `stackframes.py`'s 4,096 limit plus Watcom inlining a once-called
static is `fscheck.cpp:1053-1058`, so output never goes in a stack buffer and the dispatch table
is both the fix and POD-safe; `-os` is the default and `automation/select.o` (`:717-723`) is the
precedent that code which looks per-byte often is not; no floating point (`core/rate.h:8` —
~6.2 KB of long-double formatting for four casts); Watcom is C++98, `new T()` does not zero a
class, and `<string>`/`<vector>` stay out of shared headers — `render.h` is included by two
`app/` files.

**Deleted after review: the replayed-permission risk.** An earlier draft had the bridge set
`settleable=0` on a replayed `permission` to avoid two elements carrying `id="perm"`. The hazard
is **already closed by the guest** — `settle_perm` swaps the class *and clears the id*, and
`htmlview.h:70-74` documents exactly that — and `send_page` issues PAGESTART → `clear()`, so two
pages' blocks cannot coexist. Worse, the fix would have applied to `PendingPrompt.body`, which is
the **live** show (`permdesk.py:477`), not a replay path (`reissue()` at `:590-599` only re-sends
PERMSTATE) — stripping the id there would mean an answered prompt never collapses. Recorded
because a plausible fix for a closed hazard is worth more as a warning than as a change.

---

## 8. The `live` re-render cost

**What is new.** The guest already does `at_bottom()` + `put_innerHTML` per live update
(`htmlview.cpp:1018-1060`) on up to 4 KB (`LIVE_CAP`, `html.py:368`) every 100 ms. Added: parsing
≤4 KB of records and assembling ~5-6 KB into a `ByteSink` with no allocation per token.
`LIVE_CAP` as a *record*-byte budget is a different number from `LIVE_CAP` as an HTML budget, and
C2 re-derives it rather than carrying it across.

**How.** A3's instrumentation and corpus, the same corpus before and after. Wine for sanity only;
the VM for the 32 MB platform; the Athlon for the CPU. The P200 is out of this stage by stage-1
decision, so **write that margin into the measurement file** rather than leaving it implicit.

**What would mean "throttle it":** `render_mean > 0.2 × dom_mean`; total live time per turn up
more than 10% against A3's baseline; and an absolute floor — above **5 ms per 4 KB update** the
renderer is defective, not the platform.

**A3's baseline is measured and it corrects this section on one point.**
`research/measurements/live-before-20261012.txt`: the DOM write alone is **1.05 ms** per 4.25 KB
update on the Athlon and **3.80 ms** on the 32 MB VM, with the VM losing 9.4 MB of free memory
to one 60 KB answer and a `dom_max` of 31 ms against its own 3.8 ms mean — the shape of a box
that has started swapping. So **the ratio test binds on the FAST machine, not the slow one**,
which is the opposite of what this section assumed when it sent the measurement to three
places. `dom_mean` is 3.6× larger on the VM, so 0.2× allows 760 µs there against **213 µs on
the Athlon**, and the renderer is pure CPU while the VM's DOM is slow for reasons that are not
CPU. Evaluate the ratio on the Athlon and the memory on the VM. The 5 ms floor still has room,
but on the VM the headroom above the existing cost is 1.2 ms rather than 5.

**Fallbacks, in order:** skip the render when the body is identical to the last one (one
`memcmp`, no wire change — the bridge re-sends the whole open block, so a whitespace-only delta
produces identical bytes); lower `LIVE_CAP`; raise the coalescer interval for a client that says
it is slow, a fifth HELLO key nobody has asked for. Rendering incrementally into `#l` is
**rejected**: it needs the guest to know what changed, which is the parser coming back.

---

## 9. Verification

- **Wine proves the code**: `rendercheck.exe` (~45 cases, `CASE_COUNT` asserted exactly),
  `test_render_equivalence.py`, `test_block_vocab.py`, `test_guest_css.py`,
  `test_frame_contract.py`, `test_hello_headers.py`, `test_fakegui_conformance.py`, and the whole
  Python suite.
- **The VM proves the platform**: the template round trip through the real profile API (B3), a
  cold start with no bridge (C4), the missing-`view.css`/`view.tpl` and stale-marker paths, the
  export Save dialog and the file opened in IE5 (C3), the `live` timing on 32 MB, and a full agent
  turn with prose, a fence, a list, a table, a tool call, a tool result and a permission prompt.
  `vmship.sh` ejects before inserting, and the run starts from a `vmsnap.sh` known-good.
- **The Athlon proves the CPU**: A3's baseline and the after number, the template round trip,
  `self_update`, `uilaneprobe`, `clipprobe`.
- **The screenshot**: `viewprobe.py` before (A2/A3) and after (C2, C3), same corpus, same window,
  both committed under `research/measurements/`. The only instrument that can see a wrong or unstyled
  class, and after B3 it is also what proves the three files reached the guest.
- **Every image-moving commit**: `make -C win check` plus `cmovcheck.py` **re-read** — it fails on
  an owned-module hit that does not match the pointer-table pattern (`cmovcheck.py:156-170`) —
  with the committed total in the commit message. `make test-fast`,
  `pytest tests/python/remote -q`, `make lint`, `make wine-smoke`.

**~19 commits**, and the ordering rule above is what makes each one green: A0-A12 bridge-side
with no wire change; B1-B4 with the renderer's own bytes out of `gaston.exe`; C0 the measured cap
raise; C1-C6 the cutover.
